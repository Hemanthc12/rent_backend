# backend/app.py
import os
import json
from flask import Flask, jsonify, request
from flask_cors import CORS
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

# -----------------------
# Load environment variables from .env (optional for local dev)
# -----------------------
load_dotenv()

# -----------------------
# Config (from env vars or defaults)
# -----------------------
SHEET_NAME = os.environ.get("SHEET_NAME", "Daily Expenses")
WORKSHEET_NAME = os.environ.get("WORKSHEET_NAME", "RENT_Details")
API_KEY = os.environ.get("API_KEY", "replace_with_strong_key")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev_secret_key_change_me")

# Google Sheets JSON credentials from Render secret
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON")
if not GOOGLE_CREDS_JSON:
    raise Exception("GOOGLE_CREDS_JSON environment variable is missing!")

# -----------------------
# Flask app setup
# -----------------------
app = Flask(__name__)
CORS(app)
app.secret_key = SECRET_KEY

# Google Sheets API scopes
SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# -----------------------
# Google Sheets helper functions
# -----------------------
def get_client():
    creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDS_JSON), scopes=SCOPES)
    return gspread.authorize(creds)

def get_sheet_and_ws():
    client = get_client()
    sht = client.open(SHEET_NAME)
    ws = sht.worksheet(WORKSHEET_NAME)
    return sht, ws

# -----------------------
# API key check
# -----------------------
def require_api_key(req):
    key = req.headers.get("x-api-key") or req.args.get("api_key")
    return key == API_KEY

# -----------------------
# Root route: show nearest unpaid rents
# -----------------------
@app.route("/")
def home():
    try:
        _, ws = get_sheet_and_ws()
        data = ws.get_all_records()
        headers = ws.row_values(1)

        def find_col(header_substr):
            for h in headers:
                if header_substr.lower() in h.lower():
                    return h
            return None

        types = [
            {"id": 1, "label": "1st Sheed Amt", "amt_key_substr": "1st Sheed Amt"},
            {"id": 2, "label": "Middel Sheed Amt", "amt_key_substr": "Middel Sheed Amt"},
            {"id": 3, "label": "Pedda Sheed Amt", "amt_key_substr": "Pedda Sheed Amt"},
        ]

        out = []
        for t in types:
            amt_col = find_col(t["amt_key_substr"])
            nearest_month = None
            if amt_col:
                for row in data:
                    val = row.get(amt_col)
                    if not val or str(val).strip() == "":
                        nearest_month = row.get("MONTH")
                        break
            out.append({
                "id": t["id"],
                "label": t["label"],
                "month": nearest_month
            })

        return jsonify({
            "message": "Select rent type to pay",
            "options": out
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# -----------------------
# API: nearest unpaid (detailed)
# -----------------------
@app.route("/api/nearest-unpaid", methods=["GET"])
def nearest_unpaid():
    if not require_api_key(request):
        return jsonify({"error": "unauthorized"}), 401

    _, ws = get_sheet_and_ws()
    data = ws.get_all_records()
    headers = ws.row_values(1)

    def find_col(header_substr):
        for h in headers:
            if header_substr.lower() in h.lower():
                return h
        return None

    types = [
        {"id": 1, "label": "1st Sheed Amt", "amt_key_substr": "1st Sheed Amt", "date_key_substr": "1st Sheed Date"},
        {"id": 2, "label": "Middel Sheed Amt", "amt_key_substr": "Middel Sheed Amt", "date_key_substr": "Middle Sheed Date"},
        {"id": 3, "label": "Pedda Sheed Amt", "amt_key_substr": "Pedda Sheed Amt", "date_key_substr": "Pedda Sheed Date"},
    ]

    out = []
    for t in types:
        amt_col = find_col(t["amt_key_substr"])
        date_col = find_col(t["date_key_substr"])
        nearest_month = None
        if amt_col:
            for row in data:
                val = row.get(amt_col)
                if not val or str(val).strip() == "":
                    nearest_month = row.get("MONTH")
                    break
        out.append({
            "id": t["id"],
            "label": t["label"],
            "month": nearest_month,
            "amt_col": amt_col,
            "date_col": date_col
        })
    return jsonify(out), 200

# -----------------------
# API: update rent
# -----------------------
@app.route("/api/update", methods=["POST"])
def api_update():
    if not require_api_key(request):
        return jsonify({"error": "unauthorized"}), 401

    try:
        payload = request.get_json()
        updates = payload.get("updates", [])
        if not isinstance(updates, list) or len(updates) == 0:
            return jsonify({"error": "no updates"}), 400
    except Exception:
        return jsonify({"error": "invalid json"}), 400

    _, ws = get_sheet_and_ws()
    headers = ws.row_values(1)

    def col_index_by_header(h_name):
        for idx, h in enumerate(headers, start=1):
            if h == h_name:
                return idx
        return None

    results = []
    for upd in updates:
        month = upd.get("month")
        amt_col = upd.get("amt_col")
        date_col = upd.get("date_col")
        amount = str(upd.get("amount", "")).strip()
        paid_date = str(upd.get("paid_date", "")).strip()

        if not (month and amt_col and date_col and amount and paid_date):
            results.append({"month": month, "status": "skipped", "reason": "missing data"})
            continue

        try:
            cell = ws.find(month)
        except Exception:
            results.append({"month": month, "status": "skipped", "reason": "month not found"})
            continue

        row_num = cell.row
        amt_idx = col_index_by_header(amt_col)
        date_idx = col_index_by_header(date_col)

        if not amt_idx or not date_idx:
            results.append({"month": month, "status": "skipped", "reason": "col not found"})
            continue

        try:
            ws.update_cell(row_num, amt_idx, amount)
            ws.update_cell(row_num, date_idx, paid_date)
            results.append({"month": month, "status": "ok"})
        except Exception as e:
            results.append({"month": month, "status": "error", "reason": str(e)})

    return jsonify({"results": results}), 200

# -----------------------
# Run Flask app
# -----------------------
if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=True
    )

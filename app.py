# backend/app.py
import os
import json
from flask import Flask, jsonify, request, send_from_directory
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

# Google Sheets JSON credentials from Render secret.
# NOTE: we read it but do NOT crash at import time anymore. This lets the
# web UI load (and show a friendly error) even if the secret is missing.
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# -----------------------
# Flask app setup
# -----------------------
app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)
app.secret_key = SECRET_KEY

# Google Sheets API scopes
SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# The three rent units ("sheds"). `label` is what the UI shows; the *_substr
# values are matched (case-insensitive substring) against the sheet headers so
# small spelling differences in the sheet don't break anything.
SHED_TYPES = [
    {"id": 1, "label": "1st Shed",    "amt_key_substr": "1st Sheed Amt",    "date_key_substr": "1st Sheed Date"},
    {"id": 2, "label": "Middle Shed", "amt_key_substr": "Middel Sheed Amt", "date_key_substr": "Middle Sheed Date"},
    {"id": 3, "label": "Pedda Shed",  "amt_key_substr": "Pedda Sheed Amt",  "date_key_substr": "Pedda Sheed Date"},
]

# -----------------------
# Google Sheets helper functions
# -----------------------
def get_client():
    if not GOOGLE_CREDS_JSON:
        raise RuntimeError("GOOGLE_CREDS_JSON environment variable is missing on the server.")
    creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDS_JSON), scopes=SCOPES)
    return gspread.authorize(creds)

def get_sheet_and_ws():
    client = get_client()
    sht = client.open(SHEET_NAME)
    ws = sht.worksheet(WORKSHEET_NAME)
    return sht, ws

def find_col(headers, header_substr):
    for h in headers:
        if header_substr.lower() in h.lower():
            return h
    return None

def find_month_col(headers):
    """Locate the column that holds the month label."""
    for h in headers:
        if h.strip().upper() == "MONTH":
            return h
    return find_col(headers, "month")

def resolve_sheds(headers):
    """Return shed definitions with their real (resolved) header names."""
    out = []
    for t in SHED_TYPES:
        out.append({
            "id": t["id"],
            "label": t["label"],
            "amt_col": find_col(headers, t["amt_key_substr"]),
            "date_col": find_col(headers, t["date_key_substr"]),
        })
    return out

def is_blank(val):
    return val is None or str(val).strip() == ""

# -----------------------
# API key check
# -----------------------
def require_api_key(req):
    key = req.headers.get("x-api-key") or req.args.get("api_key")
    return key == API_KEY

# -----------------------
# Frontend (single page app)
# -----------------------
@app.route("/")
def home():
    """Serve the web UI. Falls back to a hint if the file is missing."""
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return send_from_directory(STATIC_DIR, "index.html")
    return jsonify({"message": "Rent Manager API is running, but the UI file is missing."}), 200

@app.route("/health")
def health():
    return jsonify({"status": "ok", "creds_configured": bool(GOOGLE_CREDS_JSON)}), 200

# -----------------------
# API: lightweight options (legacy public summary, no key required)
# -----------------------
@app.route("/api/options", methods=["GET"])
def api_options():
    try:
        _, ws = get_sheet_and_ws()
        data = ws.get_all_records()
        headers = ws.row_values(1)
        month_col = find_month_col(headers)

        out = []
        for t in resolve_sheds(headers):
            nearest_month = None
            if t["amt_col"]:
                for row in data:
                    if is_blank(row.get(t["amt_col"])):
                        nearest_month = row.get(month_col)
                        break
            out.append({"id": t["id"], "label": t["label"], "month": nearest_month})

        return jsonify({"message": "Select rent type to pay", "options": out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# -----------------------
# API: verify the API key (used by the UI unlock screen)
# -----------------------
@app.route("/api/verify", methods=["GET"])
def api_verify():
    if not require_api_key(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return jsonify({"ok": True}), 200

# -----------------------
# API: nearest unpaid (detailed) — kept for backward compatibility
# -----------------------
@app.route("/api/nearest-unpaid", methods=["GET"])
def nearest_unpaid():
    if not require_api_key(request):
        return jsonify({"error": "unauthorized"}), 401

    try:
        _, ws = get_sheet_and_ws()
        data = ws.get_all_records()
        headers = ws.row_values(1)
        month_col = find_month_col(headers)

        out = []
        for t in resolve_sheds(headers):
            nearest_month = None
            if t["amt_col"]:
                for row in data:
                    if is_blank(row.get(t["amt_col"])):
                        nearest_month = row.get(month_col)
                        break
            out.append({
                "id": t["id"],
                "label": t["label"],
                "month": nearest_month,
                "amt_col": t["amt_col"],
                "date_col": t["date_col"],
            })
        return jsonify(out), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# -----------------------
# API: full records (powers the dashboard + history view)
# -----------------------
@app.route("/api/records", methods=["GET"])
def api_records():
    if not require_api_key(request):
        return jsonify({"error": "unauthorized"}), 401

    try:
        _, ws = get_sheet_and_ws()
        data = ws.get_all_records()
        headers = ws.row_values(1)
        month_col = find_month_col(headers)
        sheds = resolve_sheds(headers)

        rows = []
        for row in data:
            month = row.get(month_col)
            if is_blank(month):
                continue
            cells = {}
            for s in sheds:
                amount = row.get(s["amt_col"]) if s["amt_col"] else ""
                date = row.get(s["date_col"]) if s["date_col"] else ""
                amount = "" if amount is None else str(amount).strip()
                date = "" if date is None else str(date).strip()
                cells[str(s["id"])] = {
                    "amount": amount,
                    "date": date,
                    "paid": not is_blank(amount),
                }
            rows.append({"month": str(month).strip(), "cells": cells})

        return jsonify({
            "month_col": month_col,
            "sheds": sheds,
            "rows": rows,
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

    try:
        _, ws = get_sheet_and_ws()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
            cell = ws.find(str(month))
        except Exception:
            results.append({"month": month, "status": "skipped", "reason": "month not found"})
            continue

        if cell is None:
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

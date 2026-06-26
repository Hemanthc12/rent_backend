# backend/app.py
import os
import json
import uuid
from datetime import datetime
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
TENANTS_WS = os.environ.get("TENANTS_WS", "Tenants")
ENTRIES_WS = os.environ.get("ENTRIES_WS", "RentEntries")
API_KEY = os.environ.get("API_KEY", "replace_with_strong_key")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev_secret_key_change_me")
CREATED_BY = os.environ.get("CREATED_BY", "web")
CURRENCY = os.environ.get("CURRENCY", "INR")

# Google Sheets JSON credentials from Render secret (read lazily so the UI can
# still load and show a friendly error if the secret is missing).
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# -----------------------
# Flask app setup
# -----------------------
app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)
app.secret_key = SECRET_KEY

SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# Column-name aliases (so small differences in the sheet still work).
TENANT_ID = ["tenant_id", "id", "tenantid"]
TENANT_NAME = ["tenant_name", "name", "tenant"]
TENANT_RENT = ["monthly_rent", "rent", "monthlyrent"]
TENANT_ADVANCE = ["advance", "advance_paid", "deposit", "notes"]   # 'notes' holds advance in this sheet
TENANT_PENDING = ["pending", "pending_rent", "due"]
TENANT_JOINED = ["date_joined", "joined", "date_of_joining", "join_date"]
TENANT_PHONE = ["phone", "mobile", "contact", "phone_number"]
TENANT_ROOM = ["room", "unit", "room_no", "unit_no"]
TENANT_STATUS = ["status", "active"]

# -----------------------
# Google Sheets helpers
# -----------------------
def get_client():
    if not GOOGLE_CREDS_JSON:
        raise RuntimeError("GOOGLE_CREDS_JSON environment variable is missing on the server.")
    creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDS_JSON), scopes=SCOPES)
    return gspread.authorize(creds)

_RESOLVED_SHEET_ID = None  # cache the spreadsheet that actually holds our tabs

def _has_tab(sh, name):
    n = name.strip().lower()
    try:
        return any(ws.title.strip().lower() == n for ws in sh.worksheets())
    except Exception:
        return False

def get_tab(sh, name):
    """Get a worksheet by title, matching case-insensitively and trimming spaces."""
    n = name.strip().lower()
    for ws in sh.worksheets():
        if ws.title.strip().lower() == n:
            return ws
    raise RuntimeError("Worksheet '%s' not found in the spreadsheet." % name)

def get_spreadsheet(client):
    """Find the spreadsheet holding the Tenants/RentEntries tabs. Tries the
    configured title first, then searches every spreadsheet the service account
    can access. The result is cached so we only search once."""
    global _RESOLVED_SHEET_ID
    if _RESOLVED_SHEET_ID:
        try:
            return client.open_by_key(_RESOLVED_SHEET_ID)
        except Exception:
            _RESOLVED_SHEET_ID = None
    # 1) configured title, if it actually has our tabs
    try:
        sh = client.open(SHEET_NAME)
        if _has_tab(sh, TENANTS_WS) or _has_tab(sh, ENTRIES_WS):
            _RESOLVED_SHEET_ID = sh.id
            return sh
    except Exception:
        pass
    # 2) search everything the service account can see
    try:
        for f in client.list_spreadsheet_files():
            try:
                sh = client.open_by_key(f.get("id"))
            except Exception:
                continue
            if _has_tab(sh, TENANTS_WS) and _has_tab(sh, ENTRIES_WS):
                _RESOLVED_SHEET_ID = sh.id
                return sh
    except Exception:
        pass
    # 3) last resort: the configured title (may raise if missing)
    return client.open(SHEET_NAME)

def get_ws(name):
    client = get_client()
    return get_tab(get_spreadsheet(client), name)

def header_index(headers, aliases):
    """Return (header_name, 1-based column index) for the first alias found."""
    low = [h.strip().lower() for h in headers]
    for a in aliases:
        if a in low:
            i = low.index(a)
            return headers[i], i + 1
    return None, None

def get_val(row, headers, aliases, default=""):
    name, _ = header_index(headers, aliases)
    if name is None:
        return default
    v = row.get(name, default)
    return default if v is None else v

def ensure_header(ws, headers, name):
    """Make sure a column exists; append it if missing. Returns 1-based index."""
    low = [h.strip().lower() for h in headers]
    if name.lower() in low:
        return low.index(name.lower()) + 1
    col = len(headers) + 1
    ws.update_cell(1, col, name)
    headers.append(name)
    return col

def find_row(ws, aliases, value):
    """Find the 1-based row number where a column (by alias) equals value."""
    headers = ws.row_values(1)
    _, col = header_index(headers, aliases)
    if not col:
        return None
    col_vals = ws.col_values(col)
    target = str(value).strip()
    for i, v in enumerate(col_vals[1:], start=2):
        if str(v).strip() == target:
            return i
    return None

# -----------------------
# Parsing / formatting helpers
# -----------------------
def to_num(v):
    try:
        s = str(v).replace(",", "").replace("₹", "").strip()
        return float(s) if s else 0.0
    except Exception:
        return 0.0

def is_blank(v):
    return v is None or str(v).strip() == ""

def now_synced():
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")

def current_month():
    n = datetime.now()
    return "%04d-%02d" % (n.year, n.month)

def norm_month(s):
    """Normalize a date/month string to YYYY-MM."""
    s = str(s or "").strip()
    if len(s) >= 7 and s[4] == "-":
        return s[:7]
    return s

def months_between(start_ym, end_ym):
    """Inclusive list of YYYY-MM strings from start to end."""
    try:
        sy, sm = int(start_ym[:4]), int(start_ym[5:7])
        ey, em = int(end_ym[:4]), int(end_ym[5:7])
    except Exception:
        return []
    out = []
    y, m = sy, sm
    guard = 0
    while (y, m) <= (ey, em) and guard < 600:
        out.append("%04d-%02d" % (y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
        guard += 1
    return out

# -----------------------
# Core: read tenants + entries, compute auto-pending
# -----------------------
def read_entries():
    ws = get_ws(ENTRIES_WS)
    return ws.get_all_records()

def build_state():
    client = get_client()
    sh = get_spreadsheet(client)
    tws = get_tab(sh, TENANTS_WS)
    theaders = tws.row_values(1)
    trecords = tws.get_all_records()
    entries = get_tab(sh, ENTRIES_WS).get_all_records()
    cmonth = current_month()

    # group entries by tenant_id
    by_tenant = {}
    for e in entries:
        tid = str(e.get("tenant_id", "")).strip()
        if not tid:
            continue
        by_tenant.setdefault(tid, []).append(e)

    tenants = []
    sum_rent = sum_adv = sum_pending = collected_month = collected_total = 0.0
    pending_tenants = 0

    for r in trecords:
        tid = str(get_val(r, theaders, TENANT_ID)).strip()
        if not tid:
            continue
        name = str(get_val(r, theaders, TENANT_NAME)).strip()
        rent = to_num(get_val(r, theaders, TENANT_RENT))
        advance = to_num(get_val(r, theaders, TENANT_ADVANCE))
        joined = str(get_val(r, theaders, TENANT_JOINED)).strip()
        phone = str(get_val(r, theaders, TENANT_PHONE)).strip()
        room = str(get_val(r, theaders, TENANT_ROOM)).strip()
        status = str(get_val(r, theaders, TENANT_STATUS)).strip() or "active"

        te = by_tenant.get(tid, [])
        paid_months = {}
        total_paid = 0.0
        last_date = ""
        for e in te:
            amt = to_num(e.get("amount"))
            total_paid += amt
            collected_total += amt
            fm = norm_month(e.get("for_month"))
            if fm:
                paid_months[fm] = paid_months.get(fm, 0.0) + amt
            if fm == cmonth:
                collected_month += amt
            dp = str(e.get("date_paid", "")).strip()
            if dp > last_date:
                last_date = dp

        # join month: explicit date_joined, else earliest paid month, else this month
        if joined:
            join_m = norm_month(joined)
        elif paid_months:
            join_m = min(paid_months.keys())
        else:
            join_m = cmonth

        expected = months_between(join_m, cmonth)
        pending_months = [m for m in expected if m not in paid_months]
        pending_amt = len(pending_months) * rent

        if status.lower() in ("inactive", "left", "moved", "moved out", "no"):
            pending_amt = 0
            pending_months = []

        if pending_amt > 0:
            pending_tenants += 1

        sum_rent += rent
        sum_adv += advance
        sum_pending += pending_amt

        tenants.append({
            "tenant_id": tid,
            "tenant_name": name,
            "monthly_rent": rent,
            "advance": advance,
            "date_joined": joined or join_m,
            "phone": phone,
            "room": room,
            "status": status,
            "total_paid": total_paid,
            "paid_count": len(paid_months),
            "pending_amount": pending_amt,
            "pending_months": pending_months,
            "last_payment": last_date,
            "paid_this_month": cmonth in paid_months,
        })

    summary = {
        "tenant_count": len(tenants),
        "total_monthly_rent": sum_rent,
        "total_advance": sum_adv,
        "total_pending": sum_pending,
        "pending_tenants": pending_tenants,
        "collected_this_month": collected_month,
        "collected_total": collected_total,
        "current_month": cmonth,
        "currency": CURRENCY,
    }
    return {"tenants": tenants, "summary": summary, "current_month": cmonth, "currency": CURRENCY}

# -----------------------
# Auth
# -----------------------
def require_api_key(req):
    key = req.headers.get("x-api-key") or req.args.get("api_key")
    return key == API_KEY

def guard():
    return require_api_key(request)

# -----------------------
# Frontend + health
# -----------------------
@app.route("/")
def home():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return send_from_directory(STATIC_DIR, "index.html")
    return jsonify({"message": "Rent Manager API is running, but the UI file is missing."}), 200

@app.route("/health")
def health():
    return jsonify({"status": "ok", "creds_configured": bool(GOOGLE_CREDS_JSON)}), 200

@app.route("/api/verify", methods=["GET"])
def api_verify():
    if not guard():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return jsonify({"ok": True}), 200

@app.route("/api/debug/sheets", methods=["GET"])
def api_debug_sheets():
    """List every spreadsheet (and its tabs) the service account can access.
    Used to diagnose which file holds the Tenants/RentEntries tabs."""
    if not guard():
        return jsonify({"error": "unauthorized"}), 401
    try:
        client = get_client()
        out = {"configured_title": SHEET_NAME, "tenants_ws": TENANTS_WS,
               "entries_ws": ENTRIES_WS, "resolved_id": _RESOLVED_SHEET_ID, "files": []}
        for f in client.list_spreadsheet_files():
            item = {"name": f.get("name"), "id": f.get("id")}
            try:
                sh = client.open_by_key(f.get("id"))
                item["tabs"] = [ws.title for ws in sh.worksheets()]
            except Exception as e:
                item["error"] = str(e)
            out["files"].append(item)
        return jsonify(out), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# -----------------------
# Tenants
# -----------------------
@app.route("/api/tenants", methods=["GET"])
def api_tenants():
    if not guard():
        return jsonify({"error": "unauthorized"}), 401
    try:
        return jsonify(build_state()), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/tenants", methods=["POST"])
def api_add_tenant():
    if not guard():
        return jsonify({"error": "unauthorized"}), 401
    try:
        data = request.get_json() or {}
    except Exception:
        return jsonify({"error": "invalid json"}), 400

    name = str(data.get("tenant_name", "")).strip()
    if not name:
        return jsonify({"error": "tenant_name is required"}), 400

    try:
        ws = get_ws(TENANTS_WS)
        headers = ws.row_values(1)

        tid = str(data.get("tenant_id", "")).strip()
        if not tid:
            tid = next_tenant_id(ws, headers)

        # make sure optional columns exist before writing them
        for label, aliases in [("date_joined", TENANT_JOINED), ("phone", TENANT_PHONE),
                               ("room", TENANT_ROOM), ("status", TENANT_STATUS)]:
            if data.get(label) not in (None, "") and header_index(headers, aliases)[1] is None:
                ensure_header(ws, headers, label)

        def colname(aliases, fallback):
            n, _ = header_index(headers, aliases)
            return n or fallback

        rowmap = {
            colname(TENANT_ID, "tenant_id"): tid,
            colname(TENANT_NAME, "tenant_name"): name,
            colname(TENANT_RENT, "monthly_rent"): data.get("monthly_rent", ""),
            colname(TENANT_ADVANCE, "notes"): data.get("advance", ""),
            colname(TENANT_PENDING, "pending"): "",
        }
        if data.get("date_joined"):
            rowmap[colname(TENANT_JOINED, "date_joined")] = data.get("date_joined")
        if data.get("phone"):
            rowmap[colname(TENANT_PHONE, "phone")] = data.get("phone")
        if data.get("room"):
            rowmap[colname(TENANT_ROOM, "room")] = data.get("room")
        rowmap[colname(TENANT_STATUS, "status")] = data.get("status", "active")

        row = [rowmap.get(h, "") for h in headers]
        ws.append_row(row, value_input_option="USER_ENTERED")
        return jsonify({"ok": True, "tenant_id": tid}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/tenants/<tid>", methods=["PUT"])
def api_edit_tenant(tid):
    if not guard():
        return jsonify({"error": "unauthorized"}), 401
    try:
        data = request.get_json() or {}
    except Exception:
        return jsonify({"error": "invalid json"}), 400
    try:
        ws = get_ws(TENANTS_WS)
        headers = ws.row_values(1)
        row = find_row(ws, TENANT_ID, tid)
        if not row:
            return jsonify({"error": "tenant not found"}), 404

        field_map = [
            ("tenant_name", TENANT_NAME, "tenant_name"),
            ("monthly_rent", TENANT_RENT, "monthly_rent"),
            ("advance", TENANT_ADVANCE, "notes"),
            ("date_joined", TENANT_JOINED, "date_joined"),
            ("phone", TENANT_PHONE, "phone"),
            ("room", TENANT_ROOM, "room"),
            ("status", TENANT_STATUS, "status"),
        ]
        for key, aliases, fallback in field_map:
            if key in data:
                col = header_index(headers, aliases)[1] or ensure_header(ws, headers, fallback)
                ws.update_cell(row, col, data.get(key, ""))
        return jsonify({"ok": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/tenants/<tid>", methods=["DELETE"])
def api_delete_tenant(tid):
    if not guard():
        return jsonify({"error": "unauthorized"}), 401
    try:
        ws = get_ws(TENANTS_WS)
        row = find_row(ws, TENANT_ID, tid)
        if not row:
            return jsonify({"error": "tenant not found"}), 404
        ws.delete_rows(row)
        return jsonify({"ok": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def next_tenant_id(ws, headers):
    """Auto-generate the next 'b{n}' id (regular tenants), avoiding collisions."""
    _, col = header_index(headers, TENANT_ID)
    existing = set()
    maxb = 0
    if col:
        for v in ws.col_values(col)[1:]:
            v = str(v).strip()
            existing.add(v)
            if v[:1].lower() == "b" and v[1:].isdigit():
                maxb = max(maxb, int(v[1:]))
    n = maxb + 1
    while ("b%d" % n) in existing:
        n += 1
    return "b%d" % n

# -----------------------
# Rent entries
# -----------------------
@app.route("/api/entries", methods=["GET"])
def api_entries():
    if not guard():
        return jsonify({"error": "unauthorized"}), 401
    try:
        entries = read_entries()
        tid = request.args.get("tenant_id")
        if tid:
            entries = [e for e in entries if str(e.get("tenant_id", "")).strip() == str(tid).strip()]
        # newest first by date_paid
        entries.sort(key=lambda e: str(e.get("date_paid", "")), reverse=True)
        return jsonify({"entries": entries}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/entries", methods=["POST"])
def api_add_entry():
    if not guard():
        return jsonify({"error": "unauthorized"}), 401
    try:
        data = request.get_json() or {}
    except Exception:
        return jsonify({"error": "invalid json"}), 400

    tid = str(data.get("tenant_id", "")).strip()
    amount = data.get("amount", "")
    for_month = str(data.get("for_month", "")).strip()
    if not (tid and str(amount).strip() and for_month):
        return jsonify({"error": "tenant_id, amount and for_month are required"}), 400

    try:
        ws = get_ws(ENTRIES_WS)
        headers = ws.row_values(1)
        rowmap = {
            "id": str(uuid.uuid4()),
            "tenant_id": tid,
            "tenant_name": str(data.get("tenant_name", "")).strip(),
            "amount": amount,
            "currency": data.get("currency", CURRENCY),
            "date_paid": str(data.get("date_paid", "")).strip(),
            "for_month": for_month,
            "payment_type": str(data.get("payment_type", "cash")).strip() or "cash",
            "notes": str(data.get("notes", "")).strip(),
            "receipt_url": "",
            "synced_at": now_synced(),
            "created_by": CREATED_BY,
        }
        row = [rowmap.get(h.strip().lower(), rowmap.get(h, "")) for h in headers]
        ws.append_row(row, value_input_option="USER_ENTERED")
        return jsonify({"ok": True, "id": rowmap["id"]}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/entries/<eid>", methods=["DELETE"])
def api_delete_entry(eid):
    if not guard():
        return jsonify({"error": "unauthorized"}), 401
    try:
        ws = get_ws(ENTRIES_WS)
        row = find_row(ws, ["id"], eid)
        if not row:
            return jsonify({"error": "entry not found"}), 404
        ws.delete_rows(row)
        return jsonify({"ok": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# -----------------------
# Run
# -----------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)

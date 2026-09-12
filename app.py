# backend/app.py
import os
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
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

IST = timezone(timedelta(hours=5, minutes=30))  # India Standard Time

def now_ist():
    return datetime.now(IST)

def now_synced():
    return now_ist().strftime("%d/%m/%Y %H:%M:%S")

def current_month():
    n = now_ist()
    return "%04d-%02d" % (n.year, n.month)


def previous_month():
    """Return the previous calendar month as YYYY-MM (IST)."""
    n = now_ist()
    if n.month == 1:
        return "%04d-12" % (n.year - 1)
    return "%04d-%02d" % (n.year, n.month - 1)

def parse_join(raw):
    """Return (YYYY-MM, billing day-of-month) from a join date.
    Day defaults to 1 when only a month is known."""
    s = str(raw or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return "%04d-%02d" % (int(m.group(1)), int(m.group(2))), max(1, int(m.group(3)))
    m = re.match(r"^(\d{4})-(\d{1,2})", s)
    if m:
        return "%04d-%02d" % (int(m.group(1)), int(m.group(2))), 1
    return None, 1

def latest_owed_month(join_day):
    """Return the latest rent month whose billing period is fully complete.

    Rent periods are anchored to the tenant's join day. A period ending on
    the tenant's anniversary date is considered payable only AFTER that date.
    Example: joined on July 18 -> July rent becomes due after Aug 18.
    """
    t = now_ist()
    if t.day > join_day:
        # The previous calendar month's period ended on this month's
        # anniversary day, so it is now completed.
        y, m = t.year, t.month - 1
    else:
        # The previous calendar month's period is still running until the
        # anniversary day in the current month.
        y, m = t.year, t.month - 2
    while m <= 0:
        y -= 1
        m += 12
    return "%04d-%02d" % (y, m)

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
    last_month = previous_month()

    # group entries by tenant_id
    by_tenant = {}
    for e in entries:
        tid = str(e.get("tenant_id", "")).strip()
        if not tid:
            continue
        by_tenant.setdefault(tid, []).append(e)

    tenants = []
    sum_rent = sum_adv = sum_pending = collected_last_month = collected_total = 0.0
    pending_tenants = 0

    inactive_statuses = ("inactive", "left", "moved", "moved out", "no")

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
        active = status.lower() not in inactive_statuses

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

            if fm == last_month:
                collected_last_month += amt

            dp = str(e.get("date_paid", "")).strip()
            if dp > last_date:
                last_date = dp

        # Pending is for LAST MONTH only. However, last month's rent is
        # payable only after that tenant's billing period has completed.
        # Example: joined Jul 18 -> Jul rent runs Jul 18-Aug 17 and must NOT
        # be marked pending on Aug 14. It becomes pending after Aug 18.
        join_m, join_day = parse_join(joined)
        if not join_m:
            join_m = min(paid_months.keys()) if paid_months else cmonth
            join_day = 1

        today = now_ist()
        last_period_completed = (join_m <= last_month and today.day > join_day)
        paid_last_month = paid_months.get(last_month, 0.0)

        if active and last_period_completed:
            pending_amt = max(rent - paid_last_month, 0.0)
            pending_months = [last_month] if pending_amt > 0 else []
        else:
            pending_amt = 0.0
            pending_months = []

        if pending_amt > 0:
            pending_tenants += 1

        # Monthly rent summary is ONLY for active tenants.
        if active:
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
            "paid_last_month": last_month in paid_months,
        })

    summary = {
        "tenant_count": len(tenants),
        "total_monthly_rent": sum_rent,
        "total_advance": sum_adv,
        "total_pending": sum_pending,
        "pending_tenants": pending_tenants,
        "collected_last_month": collected_last_month,
        "collected_total": collected_total,
        "current_month": cmonth,
        "last_month": last_month,
        "currency": CURRENCY,
    }

    return {
        "tenants": tenants,
        "summary": summary,
        "current_month": cmonth,
        "last_month": last_month,
        "currency": CURRENCY,
    }

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
# Utility / property sheets
# -----------------------
METERS_WS = os.environ.get("METERS_WS", "Meters")
UTILITY_BILLS_WS = os.environ.get("UTILITY_BILLS_WS", "UtilityBills")
COMMON_EXPENSES_WS = os.environ.get("COMMON_EXPENSES_WS", "CommonExpenses")
ROOMS_WS = os.environ.get("ROOMS_WS", "Rooms")

METER_HEADERS = ["meter_id", "meter_type", "room_id", "consumer_name", "rr_number", "eb_number", "connection_type", "status", "notes"]
UTILITY_BILL_HEADERS = ["bill_id", "month", "meter_id", "meter_type", "room_id", "previous_reading", "current_reading", "units", "bill_amount", "paid", "bill_date", "paid_date", "notes", "created_at"]
COMMON_EXPENSE_HEADERS = ["expense_id", "date", "month", "category", "description", "amount", "paid_by", "notes", "created_at"]
ROOM_HEADERS = ["room_id", "series", "room_name", "status", "current_tenant_id", "notes"]

def get_or_create_ws(sh, name, headers):
    try:
        return get_tab(sh, name)
    except Exception:
        ws = sh.add_worksheet(title=name, rows=1000, cols=max(20, len(headers)))
        ws.append_row(headers, value_input_option="USER_ENTERED")
        return ws

def ensure_utility_sheets(sh):
    meters = get_or_create_ws(sh, METERS_WS, METER_HEADERS)
    bills = get_or_create_ws(sh, UTILITY_BILLS_WS, UTILITY_BILL_HEADERS)
    expenses = get_or_create_ws(sh, COMMON_EXPENSES_WS, COMMON_EXPENSE_HEADERS)
    rooms = get_or_create_ws(sh, ROOMS_WS, ROOM_HEADERS)
    migrate_legacy_utility_data(sh, meters, rooms)
    return meters, bills, expenses, rooms

def _record_by_id(ws, aliases, value):
    headers = ws.row_values(1)
    return find_row(ws, aliases, value)

def _series_from_room(room_id):
    m = re.match(r"^([A-Za-z]+)", str(room_id or "").strip())
    return m.group(1).upper() if m else "OTHER"

def migrate_legacy_utility_data(sh, meters_ws, rooms_ws):
    """One-way safe migration of legacy RR/E-Bill values from Tenants.
    Old columns are intentionally preserved. Ambiguous legacy reading columns are not
    interpreted as billing history until the user records a UtilityBill explicitly.
    """
    try:
        tws = get_tab(sh, TENANTS_WS)
        headers = tws.row_values(1)
        records = tws.get_all_records()
        rr_name, rr_col = header_index(headers, ["RR-number", "rr_number", "rr number", "rr-number"])
        eb_name, eb_col = header_index(headers, ["E-Bill", "eb_number", "e-bill", "ebill", "eb number"])
        if not rr_col and not eb_col:
            return
        existing = {str(v).strip() for v in meters_ws.col_values(1)[1:] if str(v).strip()}
        existing_rooms = {str(v).strip(): i+2 for i, v in enumerate(rooms_ws.col_values(1)[1:]) if str(v).strip()}
        for r in records:
            tid = str(get_val(r, headers, TENANT_ID)).strip()
            name = str(get_val(r, headers, TENANT_NAME)).strip()
            room = str(get_val(r, headers, TENANT_ROOM)).strip() or tid
            if room:
                status = "occupied" if tid and name and str(get_val(r, headers, TENANT_STATUS)).strip().lower() not in ("inactive","left","moved","moved out","no") else "empty"
                if room not in existing_rooms:
                    rooms_ws.append_row([room, _series_from_room(room), name or room, status, tid, "Migrated from Tenants"], value_input_option="USER_ENTERED")
                    existing_rooms[room] = 1
            rr = str(r.get(rr_name, "")).strip() if rr_name else ""
            eb = str(r.get(eb_name, "")).strip() if eb_name else ""
            if rr or eb:
                meter_room = room or "PUMP"
                meter_id = "M-" + re.sub(r"[^A-Za-z0-9_-]", "", meter_room).upper()
                if meter_id not in existing:
                    meters_ws.append_row([meter_id, "Electricity", meter_room, name or ("Common Pump" if meter_room.upper()=="PUMP" else ""), rr, eb, "Common" if meter_room.upper()=="PUMP" else "Tenant", "active", "Migrated from Tenants sheet"], value_input_option="USER_ENTERED")
                    existing.add(meter_id)
                if meter_room not in existing_rooms:
                    rooms_ws.append_row([meter_room, _series_from_room(meter_room), name or meter_room, "occupied" if tid and name else "empty", tid, "Migrated from legacy meter data"], value_input_option="USER_ENTERED")
                    existing_rooms[meter_room] = 1
    except Exception:
        # Migration must never prevent the normal app from loading.
        pass

def _bool_value(v):
    return str(v).strip().lower() in ("true", "yes", "y", "paid", "1")

def _utility_state():
    client = get_client()
    sh = get_spreadsheet(client)
    meters_ws, bills_ws, expenses_ws, rooms_ws = ensure_utility_sheets(sh)
    meters = meters_ws.get_all_records()
    bills = bills_ws.get_all_records()
    expenses = expenses_ws.get_all_records()
    cmonth = current_month()
    this_month_bills = sum(to_num(x.get("bill_amount")) for x in bills if str(x.get("month", "")).strip()[:7] == cmonth)
    unpaid_bills = sum(to_num(x.get("bill_amount")) for x in bills if not _bool_value(x.get("paid")))
    this_month_common = sum(to_num(x.get("amount")) for x in expenses if str(x.get("month", "")).strip()[:7] == cmonth)
    return {"meters": meters, "bills": bills, "expenses": expenses,
            "rooms": rooms_ws.get_all_records(),
            "summary": {"this_month_bills": this_month_bills, "unpaid_bills": unpaid_bills, "this_month_common": this_month_common, "current_month": cmonth}}

@app.route("/api/utilities", methods=["GET"])
def api_utilities():
    if not guard():
        return jsonify({"error": "unauthorized"}), 401
    try:
        return jsonify(_utility_state()), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/utilities/meters", methods=["POST"])
def api_add_meter():
    if not guard():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json() or {}
    room_id = str(data.get("room_id", "")).strip()
    if not room_id:
        return jsonify({"error": "room_id is required"}), 400
    try:
        client = get_client(); sh = get_spreadsheet(client)
        ws, _, _, _ = ensure_utility_sheets(sh)
        meter_id = str(data.get("meter_id", "")).strip() or ("M-" + re.sub(r"[^A-Za-z0-9_-]", "", room_id).upper())
        if find_row(ws, ["meter_id"], meter_id):
            return jsonify({"error": "meter_id already exists"}), 409
        row = [meter_id, data.get("meter_type", "Electricity"), room_id, data.get("consumer_name", ""), data.get("rr_number", ""), data.get("eb_number", ""), data.get("connection_type", "Tenant"), data.get("status", "active"), data.get("notes", "")]
        ws.append_row(row, value_input_option="USER_ENTERED")
        return jsonify({"ok": True, "meter_id": meter_id}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/utilities/bills", methods=["POST"])
def api_add_utility_bill():
    if not guard():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json() or {}
    meter_id = str(data.get("meter_id", "")).strip()
    month = str(data.get("month", "")).strip()[:7]
    amount = data.get("bill_amount", "")
    if not meter_id or not month or str(amount).strip() == "":
        return jsonify({"error": "meter_id, month and bill_amount are required"}), 400
    try:
        client = get_client(); sh = get_spreadsheet(client)
        _, ws, _, _ = ensure_utility_sheets(sh)
        mw = get_tab(sh, METERS_WS); mh = mw.row_values(1); mrow = find_row(mw, ["meter_id"], meter_id)
        meter = mw.row_values(mrow) if mrow else []
        m = dict(zip(mh, meter)) if meter else {}
        bill_id = str(uuid.uuid4())
        row = [bill_id, month, meter_id, m.get("meter_type", "Electricity"), m.get("room_id", ""), data.get("previous_reading", ""), data.get("current_reading", ""), data.get("units", ""), amount, "TRUE" if _bool_value(data.get("paid")) else "FALSE", data.get("bill_date", ""), data.get("paid_date", ""), data.get("notes", ""), now_synced()]
        ws.append_row(row, value_input_option="USER_ENTERED")
        return jsonify({"ok": True, "bill_id": bill_id}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/utilities/expenses", methods=["POST"])
def api_add_common_expense():
    if not guard():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json() or {}
    if not str(data.get("date", "")).strip() or not str(data.get("category", "")).strip() or to_num(data.get("amount")) <= 0:
        return jsonify({"error": "date, category and positive amount are required"}), 400
    try:
        client = get_client(); sh = get_spreadsheet(client)
        _, _, ws, _ = ensure_utility_sheets(sh)
        eid = str(uuid.uuid4())
        month = str(data.get("month", "")).strip()[:7] or str(data.get("date", ""))[:7]
        row = [eid, data.get("date", ""), month, data.get("category", ""), data.get("description", ""), data.get("amount", ""), data.get("paid_by", "Owner"), data.get("notes", ""), now_synced()]
        ws.append_row(row, value_input_option="USER_ENTERED")
        return jsonify({"ok": True, "expense_id": eid}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/rooms", methods=["GET"])
def api_rooms():
    if not guard():
        return jsonify({"error": "unauthorized"}), 401
    try:
        client = get_client(); sh = get_spreadsheet(client)
        _, _, _, ws = ensure_utility_sheets(sh)
        return jsonify({"rooms": ws.get_all_records()}), 200
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
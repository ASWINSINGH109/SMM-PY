import os
import sqlite3
import requests
from flask import Flask, request, jsonify, render_template, g

app = Flask(__name__)

# --- Config ---
API_KEY     = os.environ.get("SMM_API_KEY")
SERVICE_ID  = os.environ.get("SMM_SERVICE_ID")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN")
SMM_API_URL = "https://smmvault.in/api/v2"
DATABASE    = "smm_panel.db"

# Fail fast if critical env vars are missing
if not API_KEY:
    raise RuntimeError("SMM_API_KEY environment variable is not set.")
if not SERVICE_ID:
    raise RuntimeError("SMM_SERVICE_ID environment variable is not set.")
if not ADMIN_TOKEN:
    raise RuntimeError("ADMIN_TOKEN environment variable is not set.")

# --- Status normalization ---
STATUS_MAP = {
    "pending":     "pending",
    "in progress": "processing",
    "inprogress":  "processing",
    "processing":  "processing",
    "completed":   "completed",
    "partial":     "partial",
    "cancelled":   "cancelled",
    "canceled":    "cancelled",
}

def normalize_status(raw):
    return STATUS_MAP.get((raw or "").strip().lower(), "pending")

# --- DB helpers ---
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                balance INTEGER DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                link TEXT,
                qty INTEGER,
                status TEXT DEFAULT 'pending',
                provider_order_id TEXT
            )
        """)
        db.commit()

# --- Pages ---
@app.route("/")
def index():
    return render_template("index.html")

# --- User / Balance API ---
@app.route("/api/user", methods=["POST"])
def get_or_create_user():
    data = request.json or {}
    user_id = (data.get("user_id") or "").strip()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        db.execute("INSERT INTO users (id, balance) VALUES (?, 0)", (user_id,))
        db.commit()
        balance = 0
    else:
        balance = user["balance"]

    return jsonify({"user_id": user_id, "balance": balance})

@app.route("/api/balance", methods=["GET"])
def check_balance():
    user_id = request.args.get("user_id", "").strip()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        return jsonify({"error": "User not found"}), 404

    return jsonify({"user_id": user_id, "balance": user["balance"]})

# --- Admin: Add Balance ---
@app.route("/api/admin/add-balance", methods=["POST"])
def admin_add_balance():
    token = request.headers.get("X-Admin-Token", "")
    if token != ADMIN_TOKEN:
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json or {}
    user_id = (data.get("user_id") or "").strip()
    amount  = data.get("amount", 0)

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    try:
        amount = int(amount)
        if amount <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid amount — must be a positive integer"}), 400

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        db.execute("INSERT INTO users (id, balance) VALUES (?, ?)", (user_id, amount))
    else:
        db.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, user_id))
    db.commit()

    new_balance = db.execute("SELECT balance FROM users WHERE id = ?", (user_id,)).fetchone()["balance"]
    return jsonify({"user_id": user_id, "new_balance": new_balance})

# --- Orders ---
@app.route("/api/order", methods=["POST"])
def create_order():
    data    = request.json or {}
    user_id = (data.get("user_id") or "").strip()
    link    = (data.get("link") or "").strip()
    qty     = data.get("qty", 0)

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    if not link:
        return jsonify({"error": "link is required"}), 400

    try:
        qty = int(qty)
        if qty <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        return jsonify({"error": "qty must be a positive integer"}), 400

    db   = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        return jsonify({"error": "User not found. Please log in first."}), 404

    # Pricing: 0.02 credits per unit, minimum 1 credit
    cost = max(1, int(qty * 0.02))

    if user["balance"] < cost:
        return jsonify({
            "error": f"Insufficient balance. Need {cost} credits, you have {user['balance']}."
        }), 402

    # Call external SMM API
    try:
        resp = requests.post(SMM_API_URL, data={
            "key":      API_KEY,
            "action":   "add",
            "service":  SERVICE_ID,
            "link":     link,
            "quantity": qty,
        }, timeout=20)
        resp_data = resp.json()
    except requests.exceptions.Timeout:
        return jsonify({"error": "SMM API timed out. Please try again."}), 504
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Could not reach SMM API. Check your connection."}), 502
    except Exception as e:
        return jsonify({"error": f"SMM API request failed: {str(e)}"}), 502

    # Debug: log full API response to server console
    print(f"[SMM API response] order for user={user_id}: {resp_data}")

    if "order" not in resp_data:
        error_msg = resp_data.get("error", "Unknown error from provider")
        print(f"[SMM API error] Missing 'order' key. Full response: {resp_data}")
        return jsonify({
            "error": f"SMM provider rejected the order: {error_msg}",
            "detail": resp_data
        }), 502

    provider_order_id = str(resp_data["order"])

    # Deduct balance and save order
    db.execute("UPDATE users SET balance = balance - ? WHERE id = ?", (cost, user_id))
    db.execute(
        "INSERT INTO orders (user_id, link, qty, status, provider_order_id) VALUES (?, ?, ?, 'pending', ?)",
        (user_id, link, qty, provider_order_id)
    )
    db.commit()

    return jsonify({"success": True, "provider_order_id": provider_order_id, "cost": cost})

@app.route("/api/orders", methods=["GET"])
def list_orders():
    user_id = request.args.get("user_id", "").strip()
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    db   = get_db()
    rows = db.execute(
        "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 10",
        (user_id,)
    ).fetchall()

    return jsonify({"orders": [dict(r) for r in rows]})

@app.route("/api/order/status", methods=["GET"])
def order_status():
    order_id = request.args.get("order_id", "").strip()
    user_id  = request.args.get("user_id", "").strip()

    if not order_id or not user_id:
        return jsonify({"error": "order_id and user_id are required"}), 400

    db    = get_db()
    order = db.execute(
        "SELECT * FROM orders WHERE id = ? AND user_id = ?", (order_id, user_id)
    ).fetchone()
    if not order:
        return jsonify({"error": "Order not found"}), 404

    # If no provider ID yet, return stored status
    if not order["provider_order_id"]:
        return jsonify({**dict(order), "status": order["status"]})

    # Fetch live status from provider
    try:
        resp = requests.post(SMM_API_URL, data={
            "key":    API_KEY,
            "action": "status",
            "order":  order["provider_order_id"],
        }, timeout=20)
        status_data = resp.json()
        print(f"[SMM API status] order_id={order_id}: {status_data}")
        live_status = normalize_status(status_data.get("status", ""))
    except Exception as e:
        print(f"[SMM API status error] order_id={order_id}: {str(e)}")
        live_status = order["status"]

    # Persist updated status
    db.execute("UPDATE orders SET status = ? WHERE id = ?", (live_status, order_id))
    db.commit()

    return jsonify({**dict(order), "status": live_status})

if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)

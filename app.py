import os
import sqlite3
import logging
import requests
from flask import Flask, request, jsonify, render_template, g
# CORS handled manually via after_request

# --- Logging setup ---
logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] %(levelname)s: %(message)s"
)
log = logging.getLogger(__name__)

app = Flask(__name__)
# Add CORS headers to every response
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Token"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

# --- Config ---
API_KEY     = os.environ.get("SMM_API_KEY")
SERVICE_ID  = os.environ.get("SMM_SERVICE_ID")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN")
SMM_API_URL = "https://smmvault.in/api/v2"
DATABASE    = "smm_panel.db"

# Warn but don't crash on startup — lets Railway at least serve the frontend
if not API_KEY:
    log.warning("SMM_API_KEY is not set. Order placement will fail.")
if not SERVICE_ID:
    log.warning("SMM_SERVICE_ID is not set. Order placement will fail.")
if not ADMIN_TOKEN:
    log.warning("ADMIN_TOKEN is not set. Admin routes will be inaccessible.")

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

# --- Safe JSON body parser ---
def get_json_body():
    """Safely parse JSON body regardless of Content-Type header."""
    # Try get_json first (strict), then force=True as fallback
    data = request.get_json(silent=True)
    if data is None:
        data = request.get_json(force=True, silent=True)
    if data is None:
        # Last resort: parse raw data manually
        try:
            import json
            data = json.loads(request.data.decode("utf-8"))
        except Exception:
            data = {}
    return data or {}

# --- DB helpers ---
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")  # Better concurrent access
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
        log.info("Database initialized successfully.")

# Init DB at module level — works with gunicorn (Railway) and direct python run
try:
    init_db()
except Exception as e:
    log.error(f"DB init failed: {e}")

# --- Health check (useful for Railway) ---
@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200

# --- Pages ---
@app.route("/")
def index():
    return render_template("index.html")

# --- User / Balance API ---
@app.route("/api/user", methods=["POST"])
def get_or_create_user():
    try:
        data    = get_json_body()
        user_id = (data.get("user_id") or "").strip()
        log.debug(f"[/api/user] Received body: {data}")

        if not user_id:
            return jsonify({"error": "user_id is required"}), 400

        db   = get_db()
        user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            db.execute("INSERT INTO users (id, balance) VALUES (?, 0)", (user_id,))
            db.commit()
            balance = 0
            log.info(f"[/api/user] Created new user: {user_id}")
        else:
            balance = user["balance"]
            log.info(f"[/api/user] Existing user: {user_id}, balance={balance}")

        return jsonify({"user_id": user_id, "balance": balance})

    except Exception as e:
        log.exception(f"[/api/user] Unexpected error: {e}")
        return jsonify({"error": "Internal server error", "detail": str(e)}), 500

@app.route("/api/balance", methods=["GET"])
def check_balance():
    try:
        user_id = request.args.get("user_id", "").strip()
        if not user_id:
            return jsonify({"error": "user_id is required"}), 400

        db   = get_db()
        user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return jsonify({"error": "User not found"}), 404

        return jsonify({"user_id": user_id, "balance": user["balance"]})

    except Exception as e:
        log.exception(f"[/api/balance] Unexpected error: {e}")
        return jsonify({"error": "Internal server error", "detail": str(e)}), 500

# --- Admin: Add Balance ---
@app.route("/api/admin/add-balance", methods=["POST"])
def admin_add_balance():
    try:
        token = request.headers.get("X-Admin-Token", "")
        if not ADMIN_TOKEN:
            return jsonify({"error": "Admin token not configured on server"}), 503
        if token != ADMIN_TOKEN:
            log.warning("[/api/admin/add-balance] Unauthorized access attempt.")
            return jsonify({"error": "Unauthorized"}), 403

        data    = get_json_body()
        user_id = (data.get("user_id") or "").strip()
        amount  = data.get("amount", 0)
        log.debug(f"[/api/admin/add-balance] body={data}")

        if not user_id:
            return jsonify({"error": "user_id is required"}), 400
        try:
            amount = int(amount)
            if amount <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid amount — must be a positive integer"}), 400

        db   = get_db()
        user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            db.execute("INSERT INTO users (id, balance) VALUES (?, ?)", (user_id, amount))
        else:
            db.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, user_id))
        db.commit()

        new_balance = db.execute(
            "SELECT balance FROM users WHERE id = ?", (user_id,)
        ).fetchone()["balance"]
        log.info(f"[/api/admin/add-balance] Added {amount} to {user_id}. New balance: {new_balance}")
        return jsonify({"user_id": user_id, "new_balance": new_balance})

    except Exception as e:
        log.exception(f"[/api/admin/add-balance] Unexpected error: {e}")
        return jsonify({"error": "Internal server error", "detail": str(e)}), 500

# --- Orders ---
@app.route("/api/order", methods=["POST"])
def create_order():
    try:
        data    = get_json_body()
        user_id = (data.get("user_id") or "").strip()
        link    = (data.get("link") or "").strip()
        qty     = data.get("qty", 0)
        log.debug(f"[/api/order] body={data}")

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

        if not API_KEY or not SERVICE_ID:
            return jsonify({"error": "SMM API is not configured on the server."}), 503

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
            resp.raise_for_status()
            resp_data = resp.json()
        except requests.exceptions.Timeout:
            return jsonify({"error": "SMM API timed out. Please try again."}), 504
        except requests.exceptions.ConnectionError:
            return jsonify({"error": "Could not reach SMM API. Check your connection."}), 502
        except requests.exceptions.HTTPError as e:
            return jsonify({"error": f"SMM API HTTP error: {str(e)}"}), 502
        except Exception as e:
            return jsonify({"error": f"SMM API request failed: {str(e)}"}), 502

        log.info(f"[/api/order] SMM API response for user={user_id}: {resp_data}")

        if "order" not in resp_data:
            error_msg = resp_data.get("error", "Unknown error from provider")
            log.error(f"[/api/order] Missing 'order' key. Full response: {resp_data}")
            return jsonify({
                "error": f"SMM provider rejected the order: {error_msg}",
                "detail": resp_data
            }), 502

        provider_order_id = str(resp_data["order"])

        db.execute("UPDATE users SET balance = balance - ? WHERE id = ?", (cost, user_id))
        db.execute(
            "INSERT INTO orders (user_id, link, qty, status, provider_order_id) VALUES (?, ?, ?, 'pending', ?)",
            (user_id, link, qty, provider_order_id)
        )
        db.commit()
        log.info(f"[/api/order] Order saved. provider_id={provider_order_id}, cost={cost}")

        return jsonify({"success": True, "provider_order_id": provider_order_id, "cost": cost})

    except Exception as e:
        log.exception(f"[/api/order] Unexpected error: {e}")
        return jsonify({"error": "Internal server error", "detail": str(e)}), 500

@app.route("/api/orders", methods=["GET"])
def list_orders():
    try:
        user_id = request.args.get("user_id", "").strip()
        if not user_id:
            return jsonify({"error": "user_id is required"}), 400

        db   = get_db()
        rows = db.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 10",
            (user_id,)
        ).fetchall()

        return jsonify({"orders": [dict(r) for r in rows]})

    except Exception as e:
        log.exception(f"[/api/orders] Unexpected error: {e}")
        return jsonify({"error": "Internal server error", "detail": str(e)}), 500

@app.route("/api/order/status", methods=["GET"])
def order_status():
    try:
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
            log.info(f"[/api/order/status] order_id={order_id}: {status_data}")
            live_status = normalize_status(status_data.get("status", ""))
        except Exception as e:
            log.warning(f"[/api/order/status] Could not fetch from provider: {e}")
            live_status = order["status"]

        db.execute("UPDATE orders SET status = ? WHERE id = ?", (live_status, order_id))
        db.commit()

        return jsonify({**dict(order), "status": live_status})

    except Exception as e:
        log.exception(f"[/api/order/status] Unexpected error: {e}")
        return jsonify({"error": "Internal server error", "detail": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

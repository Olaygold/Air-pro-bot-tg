
import os
import sys
import json
import logging
import threading
import subprocess
import requests
from flask import Flask, render_template, request, redirect, session
import firebase_admin
from firebase_admin import credentials, db
from dotenv import load_dotenv
from a2wsgi import WSGIMiddleware

# ──────────────────────────────────────────────
# CONFIG & LOGGING
# ──────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("AdminWeb")

# ──────────────────────────────────────────────
# FIREBASE SETUP
# ──────────────────────────────────────────────
firebase_credentials = os.getenv("FIREBASE_CREDENTIALS")
if not firebase_credentials:
    raise Exception("Missing FIREBASE_CREDENTIALS environment variable!")

try:
    firebase_clean = firebase_credentials.encode().decode("unicode_escape")
    firebase_config = json.loads(firebase_clean)
except Exception:
    firebase_config = json.loads(firebase_credentials)

if "private_key" in firebase_config:
    firebase_config["private_key"] = firebase_config["private_key"].replace("\\n", "\n")

if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_config)
    firebase_admin.initialize_app(cred, {
        "databaseURL": os.getenv("FIREBASE_URL")
    })
    logger.info("✅ Firebase connected for Web Admin.")

# ──────────────────────────────────────────────
# FLASK SETUP
# ──────────────────────────────────────────────
flask_app = Flask(__name__)
flask_app.secret_key = os.getenv("FLASK_SECRET", "supersecretkey_change_me")

BOT_TOKEN = os.getenv("BOT_TOKEN")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# ──────────────────────────────────────────────
# BACKGROUND BOT PROCESS STARTER
# ──────────────────────────────────────────────
def start_bot_process():
    """Runs bot/main.py in the background alongside this web panel on Render."""
    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        # Look for bot/main.py in parent directory or same directory
        bot_paths = [
            os.path.join(current_dir, "..", "bot", "main.py"),
            os.path.join(current_dir, "bot", "main.py"),
            os.path.join(current_dir, "main.py")
        ]
        
        bot_script = None
        for path in bot_paths:
            if os.path.exists(path):
                bot_script = os.path.abspath(path)
                break

        if bot_script:
            logger.info(f"🤖 Launching Telegram Bot process from: {bot_script}")
            subprocess.Popen([sys.executable, bot_script])
        else:
            logger.warning("⚠️ bot/main.py path not found. Please ensure bot folder exists.")
    except Exception as e:
        logger.error(f"❌ Failed to spawn Telegram bot process: {e}")

# Start Telegram Bot in background daemon thread
threading.Thread(target=start_bot_process, daemon=True).start()

# ──────────────────────────────────────────────
# TELEGRAM NOTIFIER HELPER
# ──────────────────────────────────────────────
def notify_user(chat_id, text):
    if not chat_id:
        return
    try:
        requests.post(TG_API, json={
            "chat_id": int(chat_id),
            "text": text,
            "parse_mode": "Markdown"
        }, timeout=10)
    except Exception as e:
        logger.error(f"Failed to notify user {chat_id}: {e}")

# ──────────────────────────────────────────────
# WEB ROUTES
# ──────────────────────────────────────────────
@flask_app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form.get("username", "").strip()
        pwd = request.form.get("password", "").strip()
        if user == os.getenv("ADMIN_USER") and pwd == os.getenv("ADMIN_PASS"):
            session["admin"] = True
            return redirect("/dashboard")
        return render_template("login.html", error="Invalid Username or Password")
    return render_template("login.html")

@flask_app.route("/dashboard")
def dashboard():
    if not session.get("admin"):
        return redirect("/")
    
    # Read both pending and old format withdrawals
    pending_withdrawals = db.reference("pending_withdrawals").get() or {}
    legacy_withdrawals = db.reference("withdrawals").get() or {}
    
    # Merge for rendering
    all_withdrawals = {**legacy_withdrawals, **pending_withdrawals}
    return render_template("dashboard.html", withdrawals=all_withdrawals)

@flask_app.route("/mark_paid/<withdrawal_id>", methods=["POST"])
def mark_paid(withdrawal_id):
    if not session.get("admin"):
        return redirect("/")

    # Check pending_withdrawals first, fallback to withdrawals
    ref_pending = db.reference(f"pending_withdrawals/{withdrawal_id}")
    ref_legacy = db.reference(f"withdrawals/{withdrawal_id}")
    
    data = ref_pending.get() or ref_legacy.get()

    if data:
        user_id = data.get("user_id") or data.get("telegram_id")
        amount = data.get("amount", 0)
        bank = data.get("bank", "Bank")
        account = data.get("account", "")

        # Update in pending/legacy tables
        if ref_pending.get():
            ref_pending.delete()
        if ref_legacy.get():
            ref_legacy.update({"status": "Paid"})

        # Update user's personal withdrawal log
        if user_id:
            user_data = db.reference(f"users/{user_id}").get() or {}
            withdrawals = user_data.get("withdrawals", [])
            for w in withdrawals:
                if w.get("req_id") == withdrawal_id or w.get("w_id") == withdrawal_id:
                    w["status"] = "paid_approved"
                    break
            db.reference(f"users/{user_id}").update({"withdrawals": withdrawals})

            # Notify user on Telegram
            notify_user(
                user_id,
                f"🎉 *Cash Withdrawal Approved & Paid!*\n\n"
                f"💰 Amount: *₦{amount:,}*\n"
                f"🏦 Bank: {bank} - `{account}`\n\n"
                f"Your payment has been sent to your bank account."
            )
    
    return redirect("/dashboard")

@flask_app.route("/reject/<withdrawal_id>", methods=["POST"])
def reject_withdrawal(withdrawal_id):
    if not session.get("admin"):
        return redirect("/")

    ref_pending = db.reference(f"pending_withdrawals/{withdrawal_id}")
    ref_legacy = db.reference(f"withdrawals/{withdrawal_id}")
    
    data = ref_pending.get() or ref_legacy.get()

    if data:
        user_id = data.get("user_id") or data.get("telegram_id")
        amount = data.get("amount", 0)

        # Delete from pending
        if ref_pending.get():
            ref_pending.delete()
        if ref_legacy.get():
            ref_legacy.update({"status": "Rejected"})

        # Refund user wallet
        if user_id:
            user_data = db.reference(f"users/{user_id}").get() or {}
            current_bal = user_data.get("balance", 0)
            new_bal = current_bal + amount
            withdrawals = user_data.get("withdrawals", [])
            for w in withdrawals:
                if w.get("req_id") == withdrawal_id or w.get("w_id") == withdrawal_id:
                    w["status"] = "rejected_refunded"
                    break
            
            db.reference(f"users/{user_id}").update({
                "balance": new_bal,
                "withdrawals": withdrawals
            })

            notify_user(
                user_id,
                f"❌ *Cash Withdrawal Rejected*\n\n"
                f"💰 Amount: *₦{amount:,}* has been refunded back to your balance.\n"
                f"💵 New Balance: *₦{new_bal:,}*"
            )

    return redirect("/dashboard")

@flask_app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ──────────────────────────────────────────────
# ASGI EXPORT FOR UVICORN
# ──────────────────────────────────────────────
# Wrap Flask WSGI app so uvicorn runs it directly without errors:
app = WSGIMiddleware(flask_app)

if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

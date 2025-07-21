from flask import Flask, render_template, request, redirect, session
import firebase_admin
from firebase_admin import credentials, db
from dotenv import load_dotenv
import os
import requests
import json

# Load .env file
load_dotenv()

# ✅ Firebase setup
firebase_credentials = os.getenv("FIREBASE_CREDENTIALS")
if not firebase_credentials:
    raise Exception("Missing FIREBASE_CREDENTIALS environment variable!")

firebase_config = json.loads(firebase_credentials)
firebase_config["private_key"] = firebase_config["private_key"].replace("\\n", "\n")

if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_config)
    firebase_admin.initialize_app(cred, {
        "databaseURL": os.getenv("FIREBASE_URL")
    })

# ✅ Flask setup
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "supersecretkey")

# ✅ Telegram bot setup
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise Exception("Missing BOT_TOKEN environment variable!")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# ✅ Login route
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form["username"]
        pwd = request.form["password"]
        if user == os.getenv("ADMIN_USER") and pwd == os.getenv("ADMIN_PASS"):
            session["admin"] = True
            return redirect("/dashboard")
    return render_template("login.html")

# ✅ Dashboard route
@app.route("/dashboard")
def dashboard():
    if not session.get("admin"):
        return redirect("/")
    withdrawals = db.reference("withdrawals").get() or {}
    return render_template("dashboard.html", withdrawals=withdrawals)

# ✅ Mark withdrawal as paid
@app.route("/mark_paid/<withdrawal_id>", methods=["POST"])
def mark_paid(withdrawal_id):
    if not session.get("admin"):
        return redirect("/")

    withdrawal_ref = db.reference(f"withdrawals/{withdrawal_id}")
    data = withdrawal_ref.get()

    if data:
        withdrawal_ref.update({"status": "Paid"})

        user_id = data.get("telegram_id")
        amount = data.get("amount")
        if user_id:
            notify_user(user_id, f"✅ Your withdrawal of ₦{amount} has been approved and marked as PAID.")
    
    return redirect("/dashboard")

# ✅ Notify user via Telegram
def notify_user(chat_id, text):
    try:
        requests.post(TG_API, json={
            "chat_id": chat_id,
            "text": text
        })
    except Exception as e:
        print(f"Failed to notify user: {e}")

# ✅ Logout route
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ✅ Run server
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

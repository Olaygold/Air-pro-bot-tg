from flask import Flask, render_template, request, redirect, session
import firebase_admin
from firebase_admin import credentials, db
from dotenv import load_dotenv
import os
import requests
import json

load_dotenv()

# ✅ Firebase setup
# Load Firebase credentials from environment variable
firebase_config = json.loads(os.environ.get("FIREBASE_CREDENTIALS"))

# Fix private_key newlines
firebase_config["private_key"] = firebase_config["private_key"].replace("\\n", "\n")

# Initialize Firebase app only once
if not firebase_admin._apps:
    cred = credentials.Certificate(firebase_config)
    firebase_admin.initialize_app(cred, {
        "databaseURL": os.getenv("FIREBASE_URL")
    })

# ✅ Flask setup
app = Flask(__name__)
app.secret_key = "supersecret"

# ✅ Telegram bot setup
BOT_TOKEN = os.getenv("BOT_TOKEN")
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

# ✅ Dashboard
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
        # Update status
        withdrawal_ref.update({"status": "Paid"})

        # Notify user
        user_id = data.get("telegram_id")
        amount = data.get("amount")
        if user_id:
            notify_user(user_id, f"✅ Your withdrawal of ₦{amount} has been approved and marked as PAID.")
    
    return redirect("/dashboard")

# ✅ Telegram notification function
def notify_user(chat_id, text):
    try:
        requests.post(TG_API, json={
            "chat_id": chat_id,
            "text": text
        })
    except:
        pass  # Ignore notification errors

# ✅ Logout
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

if __name__ == "__main__":
    app.run(debug=False)

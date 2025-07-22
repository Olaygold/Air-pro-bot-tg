import os
import json
from flask import Flask, request
import requests
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, db
from telegram import Bot, Update
from telegram.ext import Dispatcher, CommandHandler

# Load environment variables
load_dotenv()

# Firebase setup
firebase_config = json.loads(os.environ.get("FIREBASE_CREDENTIALS"))
firebase_config["private_key"] = firebase_config["private_key"].replace("\\n", "\n")
cred = credentials.Certificate(firebase_config)

if not firebase_admin._apps:
    firebase_admin.initialize_app(cred, {
        "databaseURL": os.environ.get("FIREBASE_URL")
    })

# Flask app
app = Flask(__name__)
TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=TOKEN)

# Dispatcher
dispatcher = Dispatcher(bot=bot, update_queue=None, use_context=True)

# Constants
WHATSAPP_LINK = os.getenv("WHATSAPP_LINK")
GROUP_USERNAME = os.getenv("GROUP_USERNAME")

# Helper
def get_user_ip(update):
    try:
        return request.remote_addr
    except:
        return "unknown"

def is_user_in_group(user_id):
    url = f"https://api.telegram.org/bot{TOKEN}/getChatMember"
    params = {"chat_id": f"@{GROUP_USERNAME}", "user_id": user_id}
    response = requests.get(url, params=params)
    result = response.json()
    if result.get("ok"):
        return result["result"]["status"] in ["member", "administrator", "creator"]
    return False

# === COMMAND HANDLERS ===

def start(update, context):
    user = update.effective_user
    args = context.args
    user_id = str(user.id)
    ip_address = get_user_ip(update)
    referred_by = args[0] if args else None

    user_ref = db.reference(f"/users/{user_id}")
    if user_ref.get():
        update.message.reply_text("✅ You are already registered.")
        return

    bonus = 500
    refer_bonus = 0

    if referred_by and referred_by != user_id:
        referred_ip_ref = db.reference(f"/users/{referred_by}/referred_ips/{ip_address}")
        if not referred_ip_ref.get():
            refer_bonus = 50
            referred_ip_ref.set(True)

    user_data = {
        "username": user.username or "",
        "full_name": user.full_name,
        "ip": ip_address,
        "balance": bonus,
        "referred_by": referred_by or "",
        "ref_code": user_id,
    }
    user_ref.set(user_data)

    if referred_by and refer_bonus > 0:
        ref_bal = db.reference(f"/users/{referred_by}/balance")
        current = ref_bal.get() or 0
        ref_bal.set(current + refer_bonus)

    reply = f"🎉 Welcome {user.first_name}!\n\nYou’ve received ₦{bonus} signup bonus."
    if refer_bonus:
        reply += f"\n\n👥 Your referrer earned ₦{refer_bonus}."

    reply += f"\n\n🔗 Join group: https://t.me/{GROUP_USERNAME}"
    reply += f"\n📱 WhatsApp: {WHATSAPP_LINK}"
    context.bot.send_message(chat_id=update.effective_chat.id, text=reply)

def balance(update, context):
    user_id = str(update.effective_user.id)
    user = db.reference(f"/users/{user_id}").get()
    if not user:
        update.message.reply_text("❌ You are not registered.")
        return
    bal = user.get("balance", 0)
    update.message.reply_text(f"💰 Your current balance is ₦{bal}")

def refer(update, context):
    user = update.effective_user
    user_id = str(user.id)
    link = f"https://t.me/{bot.username}?start={user_id}"
    update.message.reply_text(f"🔗 Your referral link:\n{link}")

def withdraw(update, context):
    user_id = str(update.effective_user.id)
    args = context.args
    if not args:
        update.message.reply_text("❗ Use like: /withdraw 100")
        return

    try:
        amount = int(args[0])
    except:
        update.message.reply_text("❗ Invalid amount")
        return

    user_ref = db.reference(f"/users/{user_id}")
    user = user_ref.get()
    if not user:
        update.message.reply_text("❌ You are not registered.")
        return

    balance = user.get("balance", 0)
    if amount > balance:
        update.message.reply_text("❌ Insufficient balance.")
        return

    # Deduct
    user_ref.child("balance").set(balance - amount)

    # Log withdrawal
    db.reference(f"/withdrawals/{user_id}").push({
        "amount": amount,
        "username": user.get("username", ""),
        "status": "pending"
    })

    update.message.reply_text(f"✅ Withdrawal of ₦{amount} requested. You will be paid soon!")

def history(update, context):
    user_id = str(update.effective_user.id)
    withdraws = db.reference(f"/withdrawals/{user_id}").get() or {}
    refers = db.reference(f"/users/{user_id}/referred_ips").get() or {}

    history_msg = "📜 *Your History*\n\n"
    history_msg += f"👥 Referrals: {len(refers)} users\n"
    history_msg += "💸 Withdrawals:\n"

    for w in withdraws.values():
        history_msg += f" - ₦{w['amount']} ({w['status']})\n"

    update.message.reply_text(history_msg, parse_mode="Markdown")

def setbalance(update, context):
    admin_ids = os.getenv("ADMIN_IDS", "").split(",")  # e.g. "12345,67890"
    if str(update.effective_user.id) not in admin_ids:
        update.message.reply_text("❌ You are not authorized.")
        return

    if len(context.args) < 2:
        update.message.reply_text("Use: /setbalance <username> <amount>")
        return

    username = context.args[0].replace("@", "")
    amount = int(context.args[1])

    users = db.reference("/users").get()
    for uid, u in users.items():
        if u.get("username") == username:
            db.reference(f"/users/{uid}/balance").set(amount)
            update.message.reply_text(f"✅ Set {username}'s balance to ₦{amount}")
            return

    update.message.reply_text("❌ User not found")

# === Webhook Route ===
@app.route('/webhook', methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return "ok", 200

@app.route('/')
def home():
    return "🤖 Bot is running", 200

# === Register Handlers ===
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CommandHandler("balance", balance))
dispatcher.add_handler(CommandHandler("refer", refer))
dispatcher.add_handler(CommandHandler("withdraw", withdraw))
dispatcher.add_handler(CommandHandler("history", history))
dispatcher.add_handler(CommandHandler("setbalance", setbalance))

# Run Flask App
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

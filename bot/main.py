import os
import json
import logging
import requests
from flask import Flask, request
from dotenv import load_dotenv
from firebase_admin import credentials, initialize_app, db
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)
from telegram.constants import ChatMemberStatus


# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_USERNAME = os.getenv("GROUP_USERNAME")
WHATSAPP_LINK = os.getenv("WHATSAPP_LINK")
FIREBASE_URL = os.getenv("FIREBASE_URL")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Firebase setup
firebase_credentials_raw = os.getenv("FIREBASE_CREDENTIALS")
firebase_credentials_clean = firebase_credentials_raw.encode().decode('unicode_escape')
cred_data = json.loads(firebase_credentials_clean)
cred = credentials.Certificate(cred_data)
initialize_app(cred, {"databaseURL": FIREBASE_URL})

# Flask app
app = Flask(__name__)

# Telegram Bot setup
application = Application.builder().token(BOT_TOKEN).build()

# Constants
SIGNUP_BONUS = 50
REFERRAL_BONUS = 50
MIN_WITHDRAW = 350

# Utils
def get_user_ref(user_id):
    return f"users/{user_id}"

def get_user_data(user_id):
    ref = db.reference(get_user_ref(user_id))
    return ref.get() or {}

def save_user_data(user_id, data):
    db.reference(get_user_ref(user_id)).update(data)

def has_joined_group(bot, user_id):
    try:
        member = bot.get_chat_member(chat_id=GROUP_USERNAME, user_id=user_id)
        return member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except:
        return True  # allow if it fails

def get_ip(update: Update):
    return update.effective_user.id  # fallback since IP isn't available on Telegram API

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    username = user.first_name
    ref_code = context.args[0] if context.args else None
    user_data = get_user_data(user_id)

    if user_data:
        await update.message.reply_text("✅ You're already registered.")
        return

    joined = has_joined_group(context.bot, user.id)
    if not joined:
        await update.message.reply_text(f"❌ Please join the Telegram group first: {GROUP_USERNAME}")
        return

    # Register user
    save_user_data(user_id, {
        "id": user_id,
        "username": username,
        "balance": SIGNUP_BONUS,
        "referrals": [],
        "withdrawals": [],
        "ref_by": ref_code or ""
    })

    # Reward referrer
    if ref_code and ref_code != user_id:
        ref_user = get_user_data(ref_code)
        if ref_user:
            if user_id not in ref_user.get("referrals", []):
                ref_user["balance"] = ref_user.get("balance", 0) + REFERRAL_BONUS
                ref_user.setdefault("referrals", []).append(user_id)
                save_user_data(ref_code, ref_user)

    await update.message.reply_text(
        f"🎉 Welcome {username}! You’ve received ₦{SIGNUP_BONUS} signup bonus.\n\n"
        f"👥 Join Telegram Group: https://t.me/{GROUP_USERNAME.lstrip('@')}\n"
        f"📱 WhatsApp Group (optional): {WHATSAPP_LINK}"
    )

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    balance = get_user_data(user_id).get("balance", 0)
    await update.message.reply_text(f"💰 Your current balance is ₦{balance}")

async def refer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    link = f"https://t.me/{context.bot.username}?start={user_id}"
    await update.message.reply_text(f"🔗 Your referral link:\n{link}")

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = get_user_data(user_id)
    referrals = user_data.get("referrals", [])
    withdrawals = user_data.get("withdrawals", [])

    text = f"👥 Referrals: {len(referrals)}\n📜 Withdrawal History:\n"
    if not withdrawals:
        text += "❌ No withdrawals yet."
    else:
        for w in withdrawals:
            text += f"• ₦{w['amount']} to {w['phone']} ({w['network']}) - {w['status']}\n"
    await update.message.reply_text(text)

async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = get_user_data(user_id)
    balance = user_data.get("balance", 0)

    if balance < MIN_WITHDRAW:
        await update.message.reply_text(f"❌ You need at least ₦{MIN_WITHDRAW} to withdraw.")
        return

    await update.message.reply_text("📱 Please enter your phone number for airtime:")

    context.user_data["withdraw_step"] = "phone"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("withdraw_step")
    user_id = str(update.effective_user.id)

    if step == "phone":
        context.user_data["withdraw_phone"] = update.message.text
        context.user_data["withdraw_step"] = "network"
        await update.message.reply_text("📶 Enter your network (MTN, Airtel, Glo, 9mobile):")
    elif step == "network":
        phone = context.user_data.get("withdraw_phone")
        network = update.message.text
        amount = MIN_WITHDRAW

        # Save withdrawal
        ref = db.reference(f"users/{user_id}")
        user_data = ref.get()
        withdrawals = user_data.get("withdrawals", [])
        withdrawals.append({
            "amount": amount,
            "phone": phone,
            "network": network,
            "status": "pending"
        })
        ref.update({
            "withdrawals": withdrawals,
            "balance": user_data.get("balance", 0) - amount
        })

        context.user_data.clear()

        await update.message.reply_text(
            f"✅ Withdrawal request of ₦{amount} submitted!\n📱 You will receive airtime on {phone} ({network})"
        )

# Add handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("balance", balance))
application.add_handler(CommandHandler("refer", refer))
application.add_handler(CommandHandler("history", history))
application.add_handler(CommandHandler("withdraw", withdraw))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# Flask webhook route
@app.route("/")
def home():
    return "✅ Airtime Drop Bot is running."

@app.route("/webhook", methods=["POST"])
async def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    await application.process_update(update)
    return "ok"


# Set webhook on startup
@app.before_first_request
def set_webhook():
    webhook_url = f"{WEBHOOK_URL}/webhook"
    requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
        params={"url": webhook_url}
    )

# Run Flask
if __name__ == "__main__":
    app.run(port=5000)

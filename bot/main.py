import os
import json
import firebase_admin
from firebase_admin import credentials, db
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv
from datetime import datetime
import logging

# Load environment variables
load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
FIREBASE_CREDENTIALS = json.loads(os.environ["FIREBASE_CREDENTIALS"])
FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL")
GROUP_USERNAME = os.environ.get("GROUP_USERNAME")  # e.g., "@YourGroup"



# Firebase initialization
firebase_config = json.loads(os.environ.get("FIREBASE_CREDENTIALS"))
firebase_config["private_key"] = firebase_config["private_key"].replace("\\n", "\n")
cred = credentials.Certificate(firebase_config)
firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
# Setup database references
users_ref = db.reference("users")
withdrawals_ref = db.reference("withdrawals")

# Logging
logging.basicConfig(level=logging.INFO)

# --- HANDLERS ---

# /start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)

    # Check if user exists
    user_data = users_ref.child(user_id).get()
    new_user = False
    ip = update.effective_message.effective_attachment or update.message.chat_id  # Use unique placeholder as IP
    # In real world, you’d extract from headers if using webhook

    # Referral handling
    referred_by = None
    if context.args:
        referred_by = context.args[0]
        if referred_by == user_id:
            referred_by = None  # Prevent self-referral

    if not user_data:
        new_user = True
        # Check if this IP already exists in the database
        existing_users = users_ref.get() or {}
        ip_exists = any(u.get("ip") == str(ip) for u in existing_users.values())

        # Create new user
        user_data = {
            "id": user_id,
            "username": user.username or "",
            "balance": 0,
            "referrals": [],
            "ip": str(ip),
            "referred_by": referred_by,
            "joined": str(datetime.utcnow()),
            "withdrawals": [],
        }

        # Give bonus only if referred and IP not used
        if referred_by and not ip_exists:
            ref_user = users_ref.child(referred_by).get()
            if ref_user:
                ref_user.setdefault("balance", 0)
                ref_user["balance"] += 50
                ref_user.setdefault("referrals", [])
                ref_user["referrals"].append(user.username or f"User{user_id}")
                users_ref.child(referred_by).update({
                    "balance": ref_user["balance"],
                    "referrals": ref_user["referrals"]
                })
                await update.message.reply_text("✅ Referral bonus added to your inviter.")
            else:
                await update.message.reply_text("⚠️ Referral ID invalid.")

        users_ref.child(user_id).set(user_data)
        await update.message.reply_text("🎉 Welcome! Your account has been created.")
    else:
        await update.message.reply_text("👋 Welcome back!")

    # Check if user is in the group
    try:
        member = await context.bot.get_chat_member(chat_id=GROUP_USERNAME, user_id=user.id)
        if member.status not in ["member", "administrator", "creator"]:
            await update.message.reply_text(f"❌ Please join our group {GROUP_USERNAME} before using the bot.")
            return
    except Exception:
        await update.message.reply_text("⚠️ Error verifying group membership.")
        return

    # Show balance
    balance = users_ref.child(user_id).child("balance").get()
    await update.message.reply_text(f"💰 Your current balance: {balance} credits")

# /withdraw command
async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    user_data = users_ref.child(user_id).get()

    if not user_data:
        await update.message.reply_text("⚠️ You are not registered. Use /start first.")
        return

    # Prevent multiple pending withdrawals
    for w in user_data.get("withdrawals", []):
        if w["status"] == "pending":
            await update.message.reply_text("⏳ You already have a pending withdrawal request.")
            return

    if user_data["balance"] < 100:
        await update.message.reply_text("❌ Minimum withdrawal is 100 credits.")
        return

    # Deduct and create withdrawal request
    new_balance = user_data["balance"] - 100
    users_ref.child(user_id).update({"balance": new_balance})

    withdrawal_request = {
        "user_id": user_id,
        "username": user.username,
        "amount": 100,
        "status": "pending",
        "time": str(datetime.utcnow()),
    }

    withdrawal_key = withdrawals_ref.push(withdrawal_request).key

    user_data["withdrawals"].append({
        "id": withdrawal_key,
        "amount": 100,
        "status": "pending"
    })
    users_ref.child(user_id).update({"withdrawals": user_data["withdrawals"]})

    await update.message.reply_text("✅ Withdrawal request submitted for 100 credits. Admin will review it.")

# /balance command
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    balance = users_ref.child(user_id).child("balance").get() or 0
    await update.message.reply_text(f"💰 Your balance: {balance} credits")

# /referrals command
async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    user_data = users_ref.child(user_id).get()

    if not user_data:
        await update.message.reply_text("⚠️ You are not registered.")
        return

    referral_link = f"https://t.me/{context.bot.username}?start={user_id}"
    referrals = user_data.get("referrals", [])

    await update.message.reply_text(
        f"📢 Invite friends with your referral link:\n{referral_link}\n\n"
        f"👥 You have referred: {len(referrals)} users\n"
        f"🧾 List: {', '.join(referrals) if referrals else 'None'}"
    )

# /admin placeholder
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛠 Admin panel is not implemented yet.")

# --- BOT LAUNCH ---
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("withdraw", withdraw))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("referrals", referrals))
    app.add_handler(CommandHandler("admin", admin))

    print("✅ Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()

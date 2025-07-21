import os
import json
import logging
from dotenv import load_dotenv
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, db
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# Load environment variables
load_dotenv()
BOT_TOKEN = os.environ.get("BOT_TOKEN")
FIREBASE_DB_URL = os.environ.get("FIREBASE_URL")
GROUP_USERNAME = os.environ.get("GROUP_USERNAME")  # e.g. @vvcmmbn
WHATSAPP_LINK = os.environ.get("WHATSAPP_LINK")  # e.g. https://chat.whatsapp.com/...

# Firebase initialization
firebase_config = json.loads(os.environ.get("FIREBASE_CREDENTIALS"))
firebase_config["private_key"] = firebase_config["private_key"].replace("\\n", "\n")
cred = credentials.Certificate(firebase_config)
firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})

# DB References
users_ref = db.reference("users")
withdrawals_ref = db.reference("withdrawals")

# Logging
logging.basicConfig(level=logging.INFO)

# Start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    username = user.username or f"User{user_id}"
    ip = update.message.chat_id  # Placeholder for IP tracking

    # Show bot intro and ask them to join group
    intro = (
        "🚀 *Welcome to air pro Reward Bot!*\n\n"
        "💰 Earn ₦50 instantly for joining our Telegram group.\n"
        "👥 Refer your friends to earn even more.\n"
        "🎉 Withdraw once your balance reaches ₦350.\n\n"
        "👉 Let's start by joining the Telegram group:"
    )

    button = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ I’ve Joined the Group", callback_data="verify_join")
    ]])
    await update.message.reply_text(intro, parse_mode="Markdown", reply_markup=button)

# Verify if user joined group
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    user_id = str(user.id)

    if query.data == "verify_join":
        try:
            member = await context.bot.get_chat_member(chat_id=GROUP_USERNAME, user_id=user.id)
            if member.status in ["member", "administrator", "creator"]:
                # Proceed with registration or welcome
                user_data = users_ref.child(user_id).get()
                ip = user.id

                referred_by = None
                if context.args:
                    referred_by = context.args[0]
                    if referred_by == user_id:
                        referred_by = None

                if not user_data:
                    existing_users = users_ref.get() or {}
                    ip_exists = any(u.get("ip") == str(ip) for u in existing_users.values())

                    user_data = {
                        "id": user_id,
                        "username": user.username or "",
                        "balance": 50,
                        "referrals": [],
                        "ip": str(ip),
                        "referred_by": referred_by,
                        "joined": str(datetime.utcnow()),
                        "withdrawals": [],
                    }

                    # If referred and IP not reused
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
                            await query.edit_message_text("✅ Referral bonus given to your inviter!")
                    users_ref.child(user_id).set(user_data)
                    await query.edit_message_text("🎉 You’ve been registered and got ₦50 bonus!")

                else:
                    await query.edit_message_text("👋 Welcome back!")

                # Show WhatsApp group
                whatsapp_button = InlineKeyboardMarkup([[
                    InlineKeyboardButton("📲 Join WhatsApp Group", url=WHATSAPP_LINK)
                ]])
                await context.bot.send_message(
                    chat_id=user_id,
                    text="🎁 Bonus granted! Join our WhatsApp group too for updates:",
                    reply_markup=whatsapp_button
                )

            else:
                await query.edit_message_text(f"❌ You must join {GROUP_USERNAME} to continue.")

        except Exception as e:
            await query.edit_message_text("⚠️ Couldn’t verify group membership. Try again later.")

# Show balance
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    balance = users_ref.child(user_id).child("balance").get() or 0
    await update.message.reply_text(f"💰 Your balance: ₦{balance}")

# Show referral link
async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = users_ref.child(user_id).get()
    if not user_data:
        await update.message.reply_text("⚠️ You're not registered.")
        return

    referrals = user_data.get("referrals", [])
    ref_link = f"https://t.me/{context.bot.username}?start={user_id}"
    await update.message.reply_text(
        f"📢 Invite friends with your referral link:\n`{ref_link}`\n\n"
        f"👥 You’ve referred: {len(referrals)} users\n"
        f"🧾 List: {', '.join(referrals) if referrals else 'None'}",
        parse_mode="Markdown"
    )

# Withdraw command
async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = users_ref.child(user_id).get()
    if not user_data:
        await update.message.reply_text("⚠️ You’re not registered.")
        return

    # Pending check
    for w in user_data.get("withdrawals", []):
        if w["status"] == "pending":
            await update.message.reply_text("⏳ You already have a pending withdrawal.")
            return

    if user_data["balance"] < 350:
        await update.message.reply_text("❌ You need at least ₦350 to withdraw.")
        return

    # Deduct and save
    new_balance = user_data["balance"] - 350
    users_ref.child(user_id).update({"balance": new_balance})
    withdrawal = {
        "user_id": user_id,
        "username": update.effective_user.username,
        "amount": 350,
        "status": "pending",
        "time": str(datetime.utcnow())
    }
    key = withdrawals_ref.push(withdrawal).key
    user_data["withdrawals"].append({"id": key, "amount": 350, "status": "pending"})
    users_ref.child(user_id).update({"withdrawals": user_data["withdrawals"]})

    await update.message.reply_text("✅ Withdrawal request submitted. It’ll be reviewed shortly.")

# Admin command (placeholder)
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛠 Admin panel is not implemented yet.")

# Run bot
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("referrals", referrals))
    app.add_handler(CommandHandler("withdraw", withdraw))
    app.add_handler(CommandHandler("admin", admin))
    print("✅ Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()

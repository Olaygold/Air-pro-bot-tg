import os, json, logging
from dotenv import load_dotenv
from datetime import datetime
from fastapi import FastAPI, Request
from contextlib import asynccontextmanager
from firebase_admin import credentials, initialize_app, db
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    AIORateLimiter,
)

# Load env vars
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
FIREBASE_URL = os.getenv("FIREBASE_URL")
GROUP_USERNAME = os.getenv("GROUP_USERNAME")
WHATSAPP_LINK = os.getenv("WHATSAPP_LINK")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Firebase init
firebase_config = json.loads(os.getenv("FIREBASE_CREDENTIALS"))
firebase_config["private_key"] = firebase_config["private_key"].replace("\\n", "\n")
cred = credentials.Certificate(firebase_config)
initialize_app(cred, {"databaseURL": FIREBASE_URL})

users_ref = db.reference("users")
withdrawals_ref = db.reference("withdrawals")

# Logging
logging.basicConfig(level=logging.INFO)

# Telegram bot app
telegram_app = Application.builder().token(BOT_TOKEN).rate_limiter(AIORateLimiter()).build()

# Lifespan hook for setting webhook
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🌐 Setting webhook...")
    await telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
    print("✅ Webhook set.")
    yield

# FastAPI app instance
app = FastAPI(lifespan=lifespan)

# Telegram webhook route
@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = Update.de_json(await request.json(), telegram_app.bot)
    await telegram_app.process_update(update)
    return {"status": "ok"}

# === BOT HANDLERS ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        "🚀 *Welcome to Air Pro Reward Bot!*\n\n"
        "💰 Earn ₦50 by joining our Telegram group.\n"
        "👥 Refer your friends and earn more.\n"
        "🎉 Withdraw when your balance hits ₦350.\n\n"
        "👉 Tap below to join the group:"
    )
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I’ve Joined the Group", callback_data="verify_group")]
    ])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=buttons)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    user_id = str(user.id)
    await query.answer()

    if query.data == "verify_group":
        try:
            member = await context.bot.get_chat_member(chat_id=GROUP_USERNAME, user_id=user.id)
            if member.status in ["member", "administrator", "creator"]:
                await query.edit_message_text(
                    "✅ Group join verified!\n\nNow please join our *WhatsApp group* for updates:",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Join WhatsApp Group", url=WHATSAPP_LINK)],
                        [InlineKeyboardButton("📲 I’ve Joined WhatsApp", callback_data="verify_whatsapp")]
                    ])
                )
            else:
                await query.edit_message_text("❌ You must join the group to continue.")
        except Exception as e:
            logging.error(e)
            await query.edit_message_text("⚠️ Couldn’t verify group membership. Try again later.")

    elif query.data == "verify_whatsapp":
        user_data = users_ref.child(user_id).get()
        referred_by = None
        args = context.args
        if args:
            referred_by = args[0]
            if referred_by == user_id:
                referred_by = None

        ip = str(user.id)  # Placeholder IP

        if not user_data:
            all_users = users_ref.get() or {}
            ip_used = any(u.get("ip") == ip for u in all_users.values())

            user_data = {
                "id": user_id,
                "username": user.username or f"user{user_id}",
                "balance": 50,
                "referrals": [],
                "ip": ip,
                "referred_by": referred_by,
                "joined": str(datetime.utcnow()),
                "withdrawals": []
            }

            if referred_by and not ip_used:
                ref_user = users_ref.child(referred_by).get()
                if ref_user:
                    ref_user.setdefault("balance", 0)
                    ref_user["balance"] += 50
                    ref_user.setdefault("referrals", [])
                    ref_user["referrals"].append(user.username or f"user{user_id}")
                    users_ref.child(referred_by).update(ref_user)

            users_ref.child(user_id).set(user_data)
            await query.edit_message_text("🎉 You’ve joined successfully and received ₦50 bonus!")
        else:
            await query.edit_message_text("👋 Welcome back! You've already joined.")

        await context.bot.send_message(chat_id=user_id, text="You can now use /balance, /withdraw, /referrals")

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    balance = users_ref.child(user_id).child("balance").get() or 0
    await update.message.reply_text(f"💰 Your balance is ₦{balance}")

async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = users_ref.child(user_id).get()
    if not data:
        await update.message.reply_text("❌ You're not registered.")
        return
    ref_link = f"https://t.me/{context.bot.username}?start={user_id}"
    referred = data.get("referrals", [])
    await update.message.reply_text(
        f"📢 Your referral link:\n`{ref_link}`\n\n"
        f"👥 Referrals: {len(referred)}\n"
        f"🧾 Users: {', '.join(referred) if referred else 'None'}",
        parse_mode="Markdown"
    )

async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = users_ref.child(user_id).get()
    if not data:
        await update.message.reply_text("❌ You’re not registered.")
        return

    if data.get("balance", 0) < 350:
        await update.message.reply_text("⚠️ You need ₦350 minimum to withdraw.")
        return

    for w in data.get("withdrawals", []):
        if w["status"] == "pending":
            await update.message.reply_text("⏳ You already have a pending withdrawal.")
            return

    new_balance = data["balance"] - 350
    users_ref.child(user_id).update({"balance": new_balance})
    withdrawal = {
        "user_id": user_id,
        "username": data["username"],
        "amount": 350,
        "status": "pending",
        "time": str(datetime.utcnow())
    }
    ref_key = withdrawals_ref.push(withdrawal).key
    data["withdrawals"].append({**withdrawal, "id": ref_key})
    users_ref.child(user_id).update({"withdrawals": data["withdrawals"]})

    await update.message.reply_text("✅ Withdrawal request submitted and pending approval.")

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔐 Admin panel coming soon.")

# Add handlers
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CallbackQueryHandler(button_handler))
telegram_app.add_handler(CommandHandler("balance", balance))
telegram_app.add_handler(CommandHandler("referrals", referrals))
telegram_app.add_handler(CommandHandler("withdraw", withdraw))
telegram_app.add_handler(CommandHandler("admin", admin))

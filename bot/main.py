import os
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from firebase_admin import credentials, db, initialize_app
from dotenv import load_dotenv
import datetime
import pytz
import firebase_admin

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
FIREBASE_URL = os.getenv("FIREBASE_URL")
GROUP_USERNAME = "@vvcmmbn"
WHATSAPP_LINK = "https://chat.whatsapp.com/LuidM3j71mDHzeKRZNIJpG"

# Initialize Firebase
cred = credentials.Certificate("firebase/firebase_config.json")
if not firebase_admin._apps:
    initialize_app(cred, {"databaseURL": FIREBASE_URL})

# DB References
users_ref = db.reference("users")
referrals_ref = db.reference("referrals")
withdrawals_ref = db.reference("withdrawals")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    user_id = str(user.id)

    user_ref = users_ref.child(user_id)
    if user_ref.get() is None:
        user_data = {
            "username": user.username,
            "balance": 50,
            "joined": str(datetime.datetime.now()),
            "referred_by": args[0] if args else "",
            "referrals": [],
            "withdrawals": []
        }
        user_ref.set(user_data)

        if args:
            ref_user_id = args[0]
            ref_user = users_ref.child(ref_user_id).get()
            if ref_user:
                ref_user["balance"] += 50
                ref_user["referrals"].append(user.username)
                users_ref.child(ref_user_id).set(ref_user)
                referrals_ref.push({
                    "referrer": ref_user_id,
                    "referred": user.username,
                    "timestamp": str(datetime.datetime.now())
                })

        await update.message.reply_text(
            f"✅ Welcome, {user.first_name}!
You've received ₦50 for joining.

"
            f"Now, join our WhatsApp group: {WHATSAPP_LINK}"
        )
    else:
        await update.message.reply_text("You are already registered.")

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = users_ref.child(user_id).get()
    if data:
        bal = data.get("balance", 0)
        ref_count = len(data.get("referrals", []))
        await update.message.reply_text(f"💰 Balance: ₦{bal}
👥 Referrals: {ref_count}")
    else:
        await update.message.reply_text("You are not registered. Use /start.")

async def refer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    ref_link = f"https://t.me/{context.bot.username}?start={user_id}"
    data = users_ref.child(user_id).get()
    if data:
        referrals = data.get("referrals", [])
        ref_names = "\n".join(referrals) if referrals else "No one yet"
        await update.message.reply_text(
            f"🔗 Your Referral Link:
{ref_link}

"
            f"👥 Referrals:
{ref_names}"
        )
    else:
        await update.message.reply_text("Please register using /start.")

async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = users_ref.child(user_id).get()
    now = datetime.datetime.now(pytz.timezone("Africa/Lagos"))
    weekday = now.strftime("%A")
    hour = now.hour

    if user_data:
        if user_data["balance"] < 350:
            await update.message.reply_text("Minimum ₦350 required to withdraw.")
        elif weekday != "Sunday" or not (19 <= hour < 20):
            await update.message.reply_text("Withdrawals are allowed only on Sundays between 7–8 PM.")
        else:
            context.user_data["withdraw_step"] = 1
            await update.message.reply_text("Enter your phone number to withdraw:")
    else:
        await update.message.reply_text("Use /start to register.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    step = context.user_data.get("withdraw_step")

    if step == 1:
        context.user_data["phone"] = update.message.text
        context.user_data["withdraw_step"] = 2
        await update.message.reply_text("Enter your network (MTN, Airtel, Glo, 9mobile):")
    elif step == 2:
        network = update.message.text
        phone = context.user_data["phone"]

        withdrawal = {
            "user_id": user_id,
            "username": update.effective_user.username,
            "phone": phone,
            "network": network,
            "amount": 350,
            "status": "pending",
            "timestamp": str(datetime.datetime.now())
        }
        withdrawals_ref.push(withdrawal)

        user_data = users_ref.child(user_id).get()
        user_data["balance"] -= 350
        user_data["withdrawals"].append(withdrawal)
        users_ref.child(user_id).set(user_data)

        context.user_data["withdraw_step"] = None
        await update.message.reply_text("✅ Withdrawal request submitted. Await admin approval.")

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = users_ref.child(user_id).get()
    if data:
        refs = data.get("referrals", [])
        wds = data.get("withdrawals", [])

        ref_text = "\n".join(refs) if refs else "No referrals yet"
        wd_text = "\n".join([f"{w['amount']} to {w['phone']} - {w['status']}" for w in wds]) if wds else "No withdrawals yet"
        await update.message.reply_text(f"👥 Referral History:
{ref_text}

💸 Withdrawal History:
{wd_text}")
    else:
        await update.message.reply_text("Please register using /start.")

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("balance", balance))
app.add_handler(CommandHandler("refer", refer))
app.add_handler(CommandHandler("withdraw", withdraw))
app.add_handler(CommandHandler("history", history))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.run_polling()

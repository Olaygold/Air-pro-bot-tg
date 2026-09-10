
import os
import json
import time
import random
import logging
import asyncio
import re
import uuid
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from firebase_admin import credentials, initialize_app, db
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ConversationHandler, AIORateLimiter
)
from telegram.constants import ChatMemberStatus

# ──────────────────────────────────────────────
# CONFIG & ENV
# ──────────────────────────────────────────────
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_USERNAME = os.getenv("GROUP_USERNAME", "@your_group")
WHATSAPP_LINK = os.getenv("WHATSAPP_LINK", "")
FIREBASE_URL = os.getenv("FIREBASE_URL")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
IA_CAFE_API_KEY = os.getenv("IA_CAFE_API_KEY")
ADMIN_CODES = [c.strip() for c in os.getenv("ADMIN_CODES", "").split(",") if c.strip()]

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# FIREBASE SETUP
# ──────────────────────────────────────────────
try:
    firebase_raw = os.getenv("FIREBASE_CREDENTIALS", "{}")
    firebase_clean = firebase_raw.encode().decode("unicode_escape")
    cred_data = json.loads(firebase_clean)
    cred = credentials.Certificate(cred_data)
    initialize_app(cred, {"databaseURL": FIREBASE_URL})
    logger.info("✅ Firebase initialized")
except Exception as e:
    logger.error(f"❌ Firebase init failed: {e}")

# ──────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────
SIGNUP_BONUS = 50
DAILY_BONUS = 10
MIN_WITHDRAW_AIRTIME = 350
DAILY_COOLDOWN_HOURS = 24

# Referral bonus weights: mostly 35, rarely 50
REFERRAL_BONUS_OPTIONS = [30, 35, 35, 35, 35, 35, 35, 40, 40, 45, 50]

# Withdrawal tiers
WITHDRAWAL_TIERS = {
    100: {"max_cash": 25000, "label": "💎 Diamond (100+ refs)"},
    50:  {"max_cash": 10000, "label": "🥇 Gold (50+ refs)"},
    0:   {"max_cash": 0,     "label": "🥉 Bronze (<50 refs, airtime only)"},
}

# Network prefix mapping (from IA-Café docs)
NETWORK_PREFIXES = {
    "mtn":     ["0703","0706","0803","0806","0810","0813","0814","0816","0903","0906","0913"],
    "airtel":  ["0701","0708","0802","0808","0812","0902","0907","0901","0912"],
    "glo":     ["0705","0805","0807","0811","0815","0905","0915"],
    "9mobile": ["0809","0817","0818","0908","0909"],
}

NETWORK_DISPLAY = {"mtn": "MTN", "airtel": "Airtel", "glo": "Glo", "9mobile": "9mobile"}
NETWORK_MIN_AMOUNT = {"mtn": 10, "airtel": 50, "glo": 50, "9mobile": 50}

# ──────────────────────────────────────────────
# CONVERSATION STATES
# ──────────────────────────────────────────────
CHOOSING_TYPE, AIRTIME_PHONE, AIRTIME_AMOUNT, CONFIRM_AIRTIME = range(4)
CASH_AMOUNT, CASH_BANK, CASH_ACCOUNT, CASH_NAME, CONFIRM_CASH = range(4, 9)

# ──────────────────────────────────────────────
# FLASK APP
# ──────────────────────────────────────────────
app = Flask(__name__)

# ──────────────────────────────────────────────
# TELEGRAM APPLICATION
# ──────────────────────────────────────────────
application = (
    Application.builder()
    .token(BOT_TOKEN)
    .rate_limiter(AIORateLimiter())
    .build()
)

# ──────────────────────────────────────────────
# FIREBASE HELPERS
# ──────────────────────────────────────────────
def get_user(user_id: str) -> dict:
    try:
        return db.reference(f"users/{user_id}").get() or {}
    except Exception as e:
        logger.error(f"Firebase read error for {user_id}: {e}")
        return {}

def save_user(user_id: str, data: dict):
    try:
        db.reference(f"users/{user_id}").update(data)
    except Exception as e:
        logger.error(f"Firebase write error for {user_id}: {e}")

def get_all_users() -> dict:
    try:
        return db.reference("users").get() or {}
    except Exception as e:
        logger.error(f"Firebase read all error: {e}")
        return {}

def get_pending_withdrawals() -> dict:
    try:
        return db.reference("pending_withdrawals").get() or {}
    except Exception as e:
        logger.error(f"Firebase pending read error: {e}")
        return {}

def save_pending_withdrawal(w_id: str, data: dict):
    try:
        db.reference(f"pending_withdrawals/{w_id}").set(data)
    except Exception as e:
        logger.error(f"Firebase pending write error: {e}")

def delete_pending_withdrawal(w_id: str):
    try:
        db.reference(f"pending_withdrawals/{w_id}").delete()
    except Exception as e:
        logger.error(f"Firebase pending delete error: {e}")

def is_admin(user_id: str) -> bool:
    user = get_user(user_id)
    return user.get("is_admin", False)

# ──────────────────────────────────────────────
# VALIDATION HELPERS
# ──────────────────────────────────────────────
def validate_phone(phone: str) -> str | None:
    """Validate Nigerian phone number, return normalized 11-digit or None."""
    phone = re.sub(r"[^\d]", "", phone)
    if phone.startswith("234") and len(phone) == 13:
        phone = "0" + phone[3:]
    elif phone.startswith("+234") and len(phone) == 14:
        phone = "0" + phone[4:]
    if len(phone) == 11 and phone.startswith("0"):
        return phone
    return None

def detect_network(phone: str) -> str | None:
    """Auto-detect network from phone prefix."""
    prefix = phone[:4]
    for network, prefixes in NETWORK_PREFIXES.items():
        if prefix in prefixes:
            return network
    return None

def get_user_tier(referral_count: int) -> dict:
    """Get withdrawal tier based on referral count."""
    if referral_count >= 100:
        return WITHDRAWAL_TIERS[100]
    elif referral_count >= 50:
        return WITHDRAWAL_TIERS[50]
    else:
        return WITHDRAWAL_TIERS[0]

def generate_request_id(user_id: str, network: str) -> str:
    """Generate unique request ID for IA-Café API."""
    ts = int(time.time())
    rand = uuid.uuid4().hex[:6]
    return f"air_{network}_{ts}_{user_id}_{rand}"

# ──────────────────────────────────────────────
# IA-CAFÉ AIRTIME API
# ──────────────────────────────────────────────
def purchase_airtime(phone: str, network: str, amount: int, user_id: str) -> dict:
    """Call IA-Café API to purchase airtime."""
    url = "https://iacafe.com.ng/devapi/v1/airtime"
    headers = {
        "Authorization": f"Bearer {IA_CAFE_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "request_id": generate_request_id(user_id, network),
        "phone": phone,
        "service_id": network,
        "amount": amount
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        data = resp.json()
        logger.info(f"IA-Café response: {data}")
        return data
    except Exception as e:
        logger.error(f"IA-Café API error: {e}")
        return {"code": "error", "message": str(e)}

# ──────────────────────────────────────────────
# GROUP JOIN CHECK (STRICT)
# ──────────────────────────────────────────────
async def has_joined_group(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=GROUP_USERNAME, user_id=user_id)
        return member.status in [
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER
        ]
    except Exception as e:
        logger.warning(f"Group check failed for {user_id}: {e}")
        return False  # STRICT: no bypass

# ──────────────────────────────────────────────
# MAIN MENU KEYBOARD
# ──────────────────────────────────────────────
def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Balance", callback_data="menu_balance"),
         InlineKeyboardButton("🔗 Refer", callback_data="menu_refer")],
        [InlineKeyboardButton("💸 Withdraw", callback_data="menu_withdraw"),
         InlineKeyboardButton("📅 Daily Bonus", callback_data="menu_daily")],
        [InlineKeyboardButton("📜 History", callback_data="menu_history"),
         InlineKeyboardButton("❓ Help", callback_data="menu_help")],
    ])

# ──────────────────────────────────────────────
# COMMAND: /start
# ──────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    username = user.first_name or "User"
    ref_code = context.args[0] if context.args else None

    user_data = get_user(user_id)
    if user_data:
        await update.message.reply_text(
            f"👋 Welcome back, {username}!\nUse the menu below:",
            reply_markup=main_menu_keyboard()
        )
        return

    # Check group membership
    joined = await has_joined_group(context.bot, user.id)
    if not joined:
        await update.message.reply_text(
            f"⚠️ You must join our Telegram group first!\n\n"
            f"👉 Join: https://t.me/{GROUP_USERNAME.lstrip('@')}\n\n"
            f"Then come back and type /start again."
        )
        return

    # Calculate random referral bonus for the referrer
    if ref_code and ref_code != user_id:
        ref_user = get_user(ref_code)
        if ref_user and user_id not in ref_user.get("referrals", []):
            bonus = random.choice(REFERRAL_BONUS_OPTIONS)
            new_balance = ref_user.get("balance", 0) + bonus
            referrals = ref_user.get("referrals", [])
            referrals.append(user_id)
            save_user(ref_code, {
                "balance": new_balance,
                "referrals": referrals
            })
            # Notify referrer
            try:
                await context.bot.send_message(
                    chat_id=int(ref_code),
                    text=f"🎉 You earned ₦{bonus} referral bonus!\n"
                         f"New balance: ₦{new_balance}"
                )
            except Exception:
                pass  # Referrer may have blocked bot

    # Save new user
    save_user(user_id, {
        "id": user_id,
        "username": username,
        "balance": SIGNUP_BONUS,
        "referrals": [],
        "withdrawals": [],
        "ref_by": ref_code or "",
        "last_checkin": "",
        "is_admin": False,
        "created_at": datetime.now().isoformat()
    })

    await update.message.reply_text(
        f"🎉 Welcome {username}!\n\n"
        f"✅ You've received ₦{SIGNUP_BONUS} signup bonus!\n\n"
        f"👥 Join our groups:\n"
        f"📱 Telegram: https://t.me/{GROUP_USERNAME.lstrip('@')}\n"
        f"💬 WhatsApp: {WHATSAPP_LINK}\n\n"
        f"Use the menu below to get started 👇",
        reply_markup=main_menu_keyboard()
    )

# ──────────────────────────────────────────────
# COMMAND: /help
# ──────────────────────────────────────────────
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *HOW TO USE THIS BOT*\n\n"
        "🔹 /start — Register & main menu\n"
        "🔹 /balance — Check your balance\n"
        "🔹 /refer — Get your referral link\n"
        "🔹 /daily — Claim ₦10 daily bonus\n"
        "🔹 /withdraw — Withdraw airtime or cash\n"
        "🔹 /history — View transaction history\n\n"
        "💸 *WITHDRAWAL TIERS:*\n"
        "🥉 <50 refs → Airtime only (min ₦350)\n"
        "🥇 50+ refs → Up to ₦10,000 cash\n"
        "💎 100+ refs → Up to ₦25,000 cash\n\n"
        "💡 Refer friends to unlock cash withdrawals!"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# ──────────────────────────────────────────────
# COMMAND: /balance
# ──────────────────────────────────────────────
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = get_user(user_id)
    if not user_data:
        await update.message.reply_text("❌ Please /start first.")
        return

    balance = user_data.get("balance", 0)
    refs = len(user_data.get("referrals", []))
    tier = get_user_tier(refs)

    await update.message.reply_text(
        f"💰 *Your Balance: ₦{balance}*\n\n"
        f"👥 Referrals: {refs}\n"
        f"🏆 Tier: {tier['label']}\n"
        f"💵 Max Cash Withdrawal: ₦{tier['max_cash']:,}",
        parse_mode="Markdown"
    )

# ──────────────────────────────────────────────
# COMMAND: /refer
# ──────────────────────────────────────────────
async def cmd_refer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = get_user(user_id)
    if not user_data:
        await update.message.reply_text("❌ Please /start first.")
        return

    bot_username = context.bot.username
    link = f"https://t.me/{bot_username}?start={user_id}"
    refs = len(user_data.get("referrals", []))

    await update.message.reply_text(
        f"🔗 *Your Referral Link:*\n`{link}`\n\n"
        f"👥 Referrals so far: {refs}\n"
        f"💰 Earn ₦30-50 per referral!\n\n"
        f"Share this link and earn passive income! 🚀",
        parse_mode="Markdown"
    )

# ──────────────────────────────────────────────
# COMMAND: /daily
# ──────────────────────────────────────────────
async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = get_user(user_id)
    if not user_data:
        await update.message.reply_text("❌ Please /start first.")
        return

    last_checkin = user_data.get("last_checkin", "")
    now = datetime.now()

    if last_checkin:
        last_dt = datetime.fromisoformat(last_checkin)
        diff = now - last_dt
        if diff < timedelta(hours=DAILY_COOLDOWN_HOURS):
            remaining = timedelta(hours=DAILY_COOLDOWN_HOURS) - diff
            hours_left = int(remaining.total_seconds() // 3600)
            mins_left = int((remaining.total_seconds() % 3600) // 60)
            await update.message.reply_text(
                f"⏳ You already claimed today!\n"
                f"Come back in {hours_left}h {mins_left}m"
            )
            return

    new_balance = user_data.get("balance", 0) + DAILY_BONUS
    save_user(user_id, {
        "balance": new_balance,
        "last_checkin": now.isoformat()
    })

    await update.message.reply_text(
        f"✅ Daily bonus claimed!\n"
        f"🎁 +₦{DAILY_BONUS}\n"
        f"💰 New balance: ₦{new_balance}"
    )

# ──────────────────────────────────────────────
# COMMAND: /history
# ──────────────────────────────────────────────
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = get_user(user_id)
    if not user_data:
        await update.message.reply_text("❌ Please /start first.")
        return

    withdrawals = user_data.get("withdrawals", [])
    refs = len(user_data.get("referrals", []))

    text = f"📜 *Transaction History*\n\n👥 Total Referrals: {refs}\n\n"

    if not withdrawals:
        text += "❌ No withdrawals yet."
    else:
        for w in withdrawals[-10:]:  # Last 10
            wtype = w.get("type", "airtime")
            icon = "📱" if wtype == "airtime" else "🏦"
            text += (
                f"{icon} ₦{w['amount']:,} ({wtype})\n"
                f"   Status: {w['status']}\n"
                f"   Date: {w.get('date', 'N/A')}\n\n"
            )

    await update.message.reply_text(text, parse_mode="Markdown")

# ──────────────────────────────────────────────
# WITHDRAWAL CONVERSATION HANDLER
# ──────────────────────────────────────────────
async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = get_user(user_id)

    if not user_data:
        await update.message.reply_text("❌ Please /start first.")
        return ConversationHandler.END

    balance = user_data.get("balance", 0)
    refs = len(user_data.get("referrals", []))
    tier = get_user_tier(refs)

    if balance < MIN_WITHDRAW_AIRTIME:
        await update.message.reply_text(
            f"❌ Minimum balance for withdrawal is ₦{MIN_WITHDRAW_AIRTIME}.\n"
            f"Your balance: ₦{balance}"
        )
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("📱 Airtime", callback_data="w_airtime")],
    ]

    if tier["max_cash"] > 0:
        keyboard.append([
            InlineKeyboardButton(f"🏦 Cash (up to ₦{tier['max_cash']:,})", callback_data="w_cash")
        ])
    else:
        keyboard.append([
            InlineKeyboardButton("🔒 Cash (need 50+ refs)", callback_data="w_cash_locked")
        ])

    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="w_cancel")])

    await update.message.reply_text(
        f"💸 *Withdrawal Menu*\n\n"
        f"💰 Balance: ₦{balance}\n"
        f"🏆 Tier: {tier['label']}\n\n"
        f"Choose withdrawal type:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return CHOOSING_TYPE

async def withdraw_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data

    if choice == "w_cancel" or choice == "w_cash_locked":
        if choice == "w_cash_locked":
            await query.edit_message_text(
                "🔒 Cash withdrawal requires 50+ referrals.\n"
                "Keep sharing your referral link! /refer"
            )
        else:
            await query.edit_message_text("❌ Withdrawal cancelled.")
        return ConversationHandler.END

    context.user_data["withdraw_type"] = "airtime" if choice == "w_airtime" else "cash"

    if choice == "w_airtime":
        await query.edit_message_text(
            "📱 *Airtime Withdrawal*\n\n"
            "Enter your 11-digit phone number:\n"
            "Example: `08012345678`",
            parse_mode="Markdown"
        )
        return AIRTIME_PHONE

    elif choice == "w_cash":
        user_id = str(update.effective_user.id)
        user_data = get_user(user_id)
        refs = len(user_data.get("referrals", []))
        tier = get_user_tier(refs)

        await query.edit_message_text(
            f"🏦 *Cash Withdrawal*\n\n"
            f"Max amount: ₦{tier['max_cash']:,}\n"
            f"Enter amount to withdraw (min ₦{MIN_WITHDRAW_AIRTIME}):",
            parse_mode="Markdown"
        )
        return CASH_AMOUNT

async def airtime_phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone_raw = update.message.text.strip()
    phone = validate_phone(phone_raw)

    if not phone:
        await update.message.reply_text(
            "❌ Invalid phone number.\n"
            "Enter a valid 11-digit Nigerian number.\n"
            "Example: `08012345678`",
            parse_mode="Markdown"
        )
        return AIRTIME_PHONE

    network = detect_network(phone)
    if not network:
        await update.message.reply_text(
            "❌ Could not detect network from phone number.\n"
            "Please enter a valid MTN, Airtel, Glo, or 9mobile number."
        )
        return AIRTIME_PHONE

    context.user_data["airtime_phone"] = phone
    context.user_data["airtime_network"] = network

    await update.message.reply_text(
        f"✅ Phone: `{phone}`\n"
        f"📶 Network: {NETWORK_DISPLAY[network]}\n\n"
        f"Enter amount (min ₦{NETWORK_MIN_AMOUNT[network]}, max ₦50,000):",
        parse_mode="Markdown"
    )
    return AIRTIME_AMOUNT

async def airtime_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Enter a valid number.")
        return AIRTIME_AMOUNT

    network = context.user_data.get("airtime_network", "mtn")
    min_amt = NETWORK_MIN_AMOUNT.get(network, 50)

    if amount < min_amt or amount > 50000:
        await update.message.reply_text(f"❌ Amount must be between ₦{min_amt} and ₦50,000.")
        return AIRTIME_AMOUNT

    user_id = str(update.effective_user.id)
    user_data = get_user(user_id)
    balance = user_data.get("balance", 0)

    if amount > balance:
        await update.message.reply_text(f"❌ Insufficient balance. You have ₦{balance}.")
        return AIRTIME_AMOUNT

    context.user_data["airtime_amount"] = amount
    phone = context.user_data["airtime_phone"]

    keyboard = [
        [InlineKeyboardButton("✅ Confirm", callback_data="confirm_airtime_yes")],
        [InlineKeyboardButton("❌ Cancel", callback_data="confirm_airtime_no")],
    ]

    await update.message.reply_text(
        f"📋 *Confirm Airtime Purchase*\n\n"
        f"📱 Phone: `{phone}`\n"
        f"📶 Network: {NETWORK_DISPLAY[network]}\n"
        f"💰 Amount: ₦{amount}\n\n"
        f"This will deduct ₦{amount} from your balance.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return CONFIRM_AIRTIME

async def confirm_airtime_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "confirm_airtime_no":
        await query.edit_message_text("❌ Airtime withdrawal cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    user_id = str(update.effective_user.id)
    phone = context.user_data.get("airtime_phone")
    network = context.user_data.get("airtime_network")
    amount = context.user_data.get("airtime_amount")

    if not all([phone, network, amount]):
        await query.edit_message_text("❌ Session expired. Try /withdraw again.")
        context.user_data.clear()
        return ConversationHandler.END

    # Re-check balance (prevent race condition)
    user_data = get_user(user_id)
    balance = user_data.get("balance", 0)
    if amount > balance:
        await query.edit_message_text("❌ Insufficient balance. Try again.")
        context.user_data.clear()
        return ConversationHandler.END

    # Deduct balance FIRST
    new_balance = balance - amount
    withdrawal_record = {
        "type": "airtime",
        "amount": amount,
        "phone": phone,
        "network": network,
        "status": "processing",
        "date": datetime.now().isoformat()
    }
    withdrawals = user_data.get("withdrawals", [])
    withdrawals.append(withdrawal_record)
    save_user(user_id, {
        "balance": new_balance,
        "withdrawals": withdrawals
    })

    await query.edit_message_text("⏳ Processing your airtime purchase...")

    # Call IA-Café API
    result = purchase_airtime(phone, network, amount, user_id)

    if result.get("code") == "success":
        status = result.get("data", {}).get("status", "completed-api")
        # Update withdrawal status
        withdrawals[-1]["status"] = "completed"
        withdrawals[-1]["api_order_id"] = result.get("data", {}).get("order_id", "")
        save_user(user_id, {"withdrawals": withdrawals})

        await context.bot.send_message(
            chat_id=int(user_id),
            text=f"✅ *Airtime Sent!*\n\n"
                 f"📱 {phone} ({NETWORK_DISPLAY[network]})\n"
                 f"💰 ₦{amount}\n"
                 f"📊 Status: {status}\n"
                 f"💵 New Balance: ₦{new_balance}",
            parse_mode="Markdown"
        )
    else:
        # REFUND on failure
        error_msg = result.get("message", "Unknown error")
        if isinstance(result.get("error"), dict):
            error_msg = result["error"].get("message", error_msg)

        refunds_balance = new_balance + amount
        withdrawals[-1]["status"] = f"failed: {error_msg}"
        save_user(user_id, {
            "balance": refunds_balance,
            "withdrawals": withdrawals
        })

        await context.bot.send_message(
            chat_id=int(user_id),
            text=f"❌ *Airtime Purchase Failed*\n\n"
                 f"Error: {error_msg}\n"
                 f"💰 ₦{amount} has been refunded.\n"
                 f"💵 Balance: ₦{refunds_balance}",
            parse_mode="Markdown"
        )

    context.user_data.clear()
    return ConversationHandler.END

# ── CASH WITHDRAWAL FLOW ──
async def cash_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Enter a valid number.")
        return CASH_AMOUNT

    user_id = str(update.effective_user.id)
    user_data = get_user(user_id)
    balance = user_data.get("balance", 0)
    refs = len(user_data.get("referrals", []))
    tier = get_user_tier(refs)

    if amount < MIN_WITHDRAW_AIRTIME:
        await update.message.reply_text(f"❌ Minimum withdrawal is ₦{MIN_WITHDRAW_AIRTIME}.")
        return CASH_AMOUNT

    if amount > tier["max_cash"]:
        await update.message.reply_text(f"❌ Max cash withdrawal for your tier is ₦{tier['max_cash']:,}.")
        return CASH_AMOUNT

    if amount > balance:
        await update.message.reply_text(f"❌ Insufficient balance. You have ₦{balance}.")
        return CASH_AMOUNT

    context.user_data["cash_amount"] = amount
    await update.message.reply_text("🏦 Enter your *bank name*:\nExample: `GTBank`", parse_mode="Markdown")
    return CASH_BANK

async def cash_bank_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bank = update.message.text.strip()
    if len(bank) < 3:
        await update.message.reply_text("❌ Enter a valid bank name.")
        return CASH_BANK

    context.user_data["cash_bank"] = bank
    await update.message.reply_text("🔢 Enter your *account number* (10 digits):", parse_mode="Markdown")
    return CASH_ACCOUNT

async def cash_account_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    account = re.sub(r"[^\d]", "", update.message.text.strip())
    if len(account) != 10:
        await update.message.reply_text("❌ Account number must be 10 digits.")
        return CASH_ACCOUNT

    context.user_data["cash_account"] = account
    await update.message.reply_text("👤 Enter your *account name* (as it appears on your bank):", parse_mode="Markdown")
    return CASH_NAME

async def cash_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if len(name) < 3:
        await update.message.reply_text("❌ Enter a valid account name.")
        return CASH_NAME

    context.user_data["cash_name"] = name
    amount = context.user_data["cash_amount"]
    bank = context.user_data["cash_bank"]
    account = context.user_data["cash_account"]

    keyboard = [
        [InlineKeyboardButton("✅ Confirm", callback_data="confirm_cash_yes")],
        [InlineKeyboardButton("❌ Cancel", callback_data="confirm_cash_no")],
    ]

    await update.message.reply_text(
        f"📋 *Confirm Cash Withdrawal*\n\n"
        f"💰 Amount: ₦{amount:,}\n"
        f"🏦 Bank: {bank}\n"
        f"🔢 Account: {account}\n"
        f"👤 Name: {name}\n\n"
        f"⚠️ This will be reviewed by an admin before processing.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return CONFIRM_CASH

async def confirm_cash_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "confirm_cash_no":
        await query.edit_message_text("❌ Cash withdrawal cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    user_id = str(update.effective_user.id)
    user_data = get_user(user_id)
    amount = context.user_data.get("cash_amount")
    bank = context.user_data.get("cash_bank")
    account = context.user_data.get("cash_account")
    name = context.user_data.get("cash_name")

    if not all([amount, bank, account, name]):
        await query.edit_message_text("❌ Session expired. Try /withdraw again.")
        context.user_data.clear()
        return ConversationHandler.END

    # Re-check balance
    balance = user_data.get("balance", 0)
    if amount > balance:
        await query.edit_message_text("❌ Insufficient balance.")
        context.user_data.clear()
        return ConversationHandler.END

    # Deduct balance
    new_balance = balance - amount
    w_id = f"cash_{user_id}_{int(time.time())}"

    withdrawal_record = {
        "type": "cash",
        "amount": amount,
        "bank": bank,
        "account": account,
        "account_name": name,
        "status": "pending",
        "date": datetime.now().isoformat(),
        "w_id": w_id
    }

    withdrawals = user_data.get("withdrawals", [])
    withdrawals.append(withdrawal_record)
    save_user(user_id, {
        "balance": new_balance,
        "withdrawals": withdrawals
    })

    # Save to pending queue for admin
    save_pending_withdrawal(w_id, {
        "user_id": user_id,
        "username": user_data.get("username", "Unknown"),
        "amount": amount,
        "bank": bank,
        "account": account,
        "account_name": name,
        "date": datetime.now().isoformat(),
        "status": "pending"
    })

    # Notify admins
    all_users = get_all_users()
    for uid, udata in all_users.items():
        if udata.get("is_admin"):
            try:
                await context.bot.send_message(
                    chat_id=int(uid),
                    text=f"🚨 *New Cash Withdrawal Request*\n\n"
                         f"👤 User: {user_data.get('username')} (`{user_id}`)\n"
                         f"💰 Amount: ₦{amount:,}\n"
                         f"🏦 Bank: {bank}\n"
                         f"🔢 Account: {account}\n"
                         f"👤 Name: {name}\n"
                         f"🆔 ID: `{w_id}`\n\n"
                         f"Use /approve `{w_id}` or /reject `{w_id}`",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

    await query.edit_message_text(
        f"✅ *Cash Withdrawal Submitted!*\n\n"
        f"💰 ₦{amount:,}\n"
        f"🏦 {bank} - {account}\n"
        f"📊 Status: Pending admin approval\n"
        f"💵 New Balance: ₦{new_balance}\n\n"
        f"You'll be notified when processed.",
        parse_mode="Markdown"
    )

    context.user_data.clear()
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Withdrawal cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ──────────────────────────────────────────────
# ADMIN COMMANDS
# ──────────────────────────────────────────────
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    code = context.args[0] if context.args else ""

    if not code:
        await update.message.reply_text("Usage: /admin <your_code>")
        return

    if code in ADMIN_CODES:
        save_user(user_id, {"is_admin": True})
        await update.message.reply_text(
            "✅ *Admin access granted!*\n\n"
            "Available commands:\n"
            "/pending — View pending withdrawals\n"
            "/approve <id> — Approve cash withdrawal\n"
            "/reject <id> — Reject & refund\n"
            "/stats — Bot statistics\n"
            "/broadcast <msg> — Send to all users",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Invalid admin code.")

async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_admin(user_id):
        await update.message.reply_text("❌ Admin only.")
        return

    pending = get_pending_withdrawals()
    if not pending:
        await update.message.reply_text("✅ No pending withdrawals.")
        return

    text = "📋 *Pending Cash Withdrawals*\n\n"
    for w_id, w in pending.items():
        text += (
            f"🆔 `{w_id}`\n"
            f"👤 {w.get('username')} ({w.get('user_id')})\n"
            f"💰 ₦{w.get('amount', 0):,}\n"
            f"🏦 {w.get('bank')} - {w.get('account')}\n"
            f"👤 {w.get('account_name')}\n\n"
        )

    text += "Use /approve <id> or /reject <id>"
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_admin(user_id):
        await update.message.reply_text("❌ Admin only.")
        return

    w_id = context.args[0] if context.args else ""
    if not w_id:
        await update.message.reply_text("Usage: /approve <withdrawal_id>")
        return

    pending = get_pending_withdrawals()
    if w_id not in pending:
        await update.message.reply_text("❌ Withdrawal not found.")
        return

    w = pending[w_id]
    target_user_id = w["user_id"]

    # Update user's withdrawal status
    user_data = get_user(target_user_id)
    withdrawals = user_data.get("withdrawals", [])
    for wd in withdrawals:
        if wd.get("w_id") == w_id:
            wd["status"] = "approved ✅"
            break
    save_user(target_user_id, {"withdrawals": withdrawals})

    # Remove from pending
    delete_pending_withdrawal(w_id)

    # Notify user
    try:
        await context.bot.send_message(
            chat_id=int(target_user_id),
            text=f"✅ *Withdrawal Approved!*\n\n"
                 f"💰 ₦{w['amount']:,}\n"
                 f"🏦 {w['bank']} - {w['account']}\n"
                 f"Your payment is being processed.",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    await update.message.reply_text(f"✅ Approved {w_id} for ₦{w['amount']:,}")

async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_admin(user_id):
        await update.message.reply_text("❌ Admin only.")
        return

    w_id = context.args[0] if context.args else ""
    if not w_id:
        await update.message.reply_text("Usage: /reject <withdrawal_id>")
        return

    pending = get_pending_withdrawals()
    if w_id not in pending:
        await update.message.reply_text("❌ Withdrawal not found.")
        return

    w = pending[w_id]
    target_user_id = w["user_id"]
    amount = w["amount"]

    # Refund user
    user_data = get_user(target_user_id)
    new_balance = user_data.get("balance", 0) + amount
    withdrawals = user_data.get("withdrawals", [])
    for wd in withdrawals:
        if wd.get("w_id") == w_id:
            wd["status"] = "rejected ❌ (refunded)"
            break
    save_user(target_user_id, {
        "balance": new_balance,
        "withdrawals": withdrawals
    })

    delete_pending_withdrawal(w_id)

    try:
        await context.bot.send_message(
            chat_id=int(target_user_id),
            text=f"❌ *Withdrawal Rejected*\n\n"
                 f"💰 ₦{amount:,} has been refunded.\n"
                 f"💵 New Balance: ₦{new_balance}\n\n"
                 f"Contact support if you have questions.",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    await update.message.reply_text(f"❌ Rejected {w_id}. ₦{amount:,} refunded.")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_admin(user_id):
        await update.message.reply_text("❌ Admin only.")
        return

    all_users = get_all_users()
    total_users = len(all_users)
    total_balance = sum(u.get("balance", 0) for u in all_users.values())
    total_refs = sum(len(u.get("referrals", [])) for u in all_users.values())
    pending = get_pending_withdrawals()

    await update.message.reply_text(
        f"📊 *Bot Statistics*\n\n"
        f"👥 Total Users: {total_users}\n"
        f"💰 Total Balance: ₦{total_balance:,}\n"
        f"🔗 Total Referrals: {total_refs}\n"
        f"⏳ Pending Withdrawals: {len(pending)}",
        parse_mode="Markdown"
    )

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_admin(user_id):
        await update.message.reply_text("❌ Admin only.")
        return

    message = " ".join(context.args) if context.args else ""
    if not message:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    all_users = get_all_users()
    sent = 0
    failed = 0

    for uid in all_users:
        try:
            await context.bot.send_message(chat_id=int(uid), text=message)
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(f"📢 Broadcast complete.\n✅ Sent: {sent}\n❌ Failed: {failed}")

# ──────────────────────────────────────────────
# INLINE MENU CALLBACK HANDLER
# ──────────────────────────────────────────────
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_balance":
        user_id = str(update.effective_user.id)
        user_data = get_user(user_id)
        if not user_data:
            await query.edit_message_text("❌ Please /start first.")
            return
        balance = user_data.get("balance", 0)
        refs = len(user_data.get("referrals", []))
        tier = get_user_tier(refs)
        await query.edit_message_text(
            f"💰 Balance: ₦{balance}\n👥 Referrals: {refs}\n🏆 {tier['label']}",
            reply_markup=main_menu_keyboard()
        )

    elif data == "menu_refer":
        user_id = str(update.effective_user.id)
        link = f"https://t.me/{context.bot.username}?start={user_id}"
        await query.edit_message_text(
            f"🔗 Your referral link:\n`{link}`\n\nShare & earn ₦30-50 per referral!",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )

    elif data == "menu_withdraw":
        await query.edit_message_text("💸 Use /withdraw to start a withdrawal.")

    elif data == "menu_daily":
        # Trigger daily check-in
        user_id = str(update.effective_user.id)
        user_data = get_user(user_id)
        if not user_data:
            await query.edit_message_text("❌ Please /start first.")
            return
        last_checkin = user_data.get("last_checkin", "")
        now = datetime.now()
        if last_checkin:
            last_dt = datetime.fromisoformat(last_checkin)
            if now - last_dt < timedelta(hours=DAILY_COOLDOWN_HOURS):
                remaining = timedelta(hours=DAILY_COOLDOWN_HOURS) - (now - last_dt)
                h = int(remaining.total_seconds() // 3600)
                m = int((remaining.total_seconds() % 3600) // 60)
                await query.edit_message_text(
                    f"⏳ Already claimed! Come back in {h}h {m}m",
                    reply_markup=main_menu_keyboard()
                )
                return
        new_balance = user_data.get("balance", 0) + DAILY_BONUS
        save_user(user_id, {"balance": new_balance, "last_checkin": now.isoformat()})
        await query.edit_message_text(
            f"✅ +₦{DAILY_BONUS} daily bonus!\n💰 Balance: ₦{new_balance}",
            reply_markup=main_menu_keyboard()
        )

    elif data == "menu_history":
        user_id = str(update.effective_user.id)
        user_data = get_user(user_id)
        withdrawals = user_data.get("withdrawals", []) if user_data else []
        if not withdrawals:
            await query.edit_message_text("📜 No transactions yet.", reply_markup=main_menu_keyboard())
        else:
            text = "📜 *Recent Transactions*\n\n"
            for w in withdrawals[-5:]:
                icon = "📱" if w.get("type") == "airtime" else "🏦"
                text += f"{icon} ₦{w['amount']:,} — {w['status']}\n"
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())

    elif data == "menu_help":
        await query.edit_message_text(
            "📖 Use /help for full instructions.",
            reply_markup=main_menu_keyboard()
        )

# ──────────────────────────────────────────────
# REGISTER HANDLERS
# ──────────────────────────────────────────────
withdraw_conv = ConversationHandler(
    entry_points=[CommandHandler("withdraw", withdraw_start)],
    states={
        CHOOSING_TYPE: [CallbackQueryHandler(withdraw_type_callback, pattern="^w_")],
        AIRTIME_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, airtime_phone_handler)],
        AIRTIME_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, airtime_amount_handler)],
        CONFIRM_AIRTIME: [CallbackQueryHandler(confirm_airtime_callback, pattern="^confirm_airtime_")],
        CASH_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, cash_amount_handler)],
        CASH_BANK: [MessageHandler(filters.TEXT & ~filters.COMMAND, cash_bank_handler)],
        CASH_ACCOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, cash_account_handler)],
        CASH_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, cash_name_handler)],
        CONFIRM_CASH: [CallbackQueryHandler(confirm_cash_callback, pattern="^confirm_cash_")],
    },
    fallbacks=[CommandHandler("cancel", cancel_conversation)],
    conversation_timeout=300,  # 5 min timeout
)

application.add_handler(withdraw_conv)
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("help", cmd_help))
application.add_handler(CommandHandler("balance", cmd_balance))
application.add_handler(CommandHandler("refer", cmd_refer))
application.add_handler(CommandHandler("daily", cmd_daily))
application.add_handler(CommandHandler("history", cmd_history))
application.add_handler(CommandHandler("admin", cmd_admin))
application.add_handler(CommandHandler("pending", cmd_pending))
application.add_handler(CommandHandler("approve", cmd_approve))
application.add_handler(CommandHandler("reject", cmd_reject))
application.add_handler(CommandHandler("stats", cmd_stats))
application.add_handler(CommandHandler("broadcast", cmd_broadcast))
application.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_"))

# ──────────────────────────────────────────────
# FLASK ROUTES
# ──────────────────────────────────────────────
@app.route("/")
def home():
    return "✅ Airtime Drop Bot v2.0 is running."

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    try:
        update = Update.de_json(request.get_json(force=True), application.bot)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(application.process_update(update))
        loop.close()
    except Exception as e:
        logger.error(f"Webhook error: {e}")
    return "ok", 200

# ──────────────────────────────────────────────
# STARTUP
# ──────────────────────────────────────────────
def setup_webhook():
    """Set Telegram webhook on startup."""
    url = f"{WEBHOOK_URL}/webhook"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
            json={"url": url, "allowed_updates": ["message", "callback_query"]}
        )
        logger.info(f"Webhook set: {resp.json()}")
    except Exception as e:
        logger.error(f"Webhook setup failed: {e}")

# Initialize bot application
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
loop.run_until_complete(application.initialize())
setup_webhook()

# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info(f"🚀 Starting server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

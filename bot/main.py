
import os
import json
import time
import random
import logging
import re
import uuid
import httpx
from datetime import datetime, timedelta

from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, db

from telegram import (
    Update,
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler,
    AIORateLimiter
)
from telegram.constants import ChatMemberStatus

# ──────────────────────────────────────────────
# CONFIG & LOGGING
# ──────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("AirtimeBot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
# Support CHANNEL_ID / GROUP_USERNAME and CHANNEL_INVITE_LINK / GROUP_LINK
CHANNEL_ID = os.getenv("CHANNEL_ID") or os.getenv("GROUP_USERNAME", "")
CHANNEL_INVITE_LINK = os.getenv("CHANNEL_INVITE_LINK") or os.getenv("GROUP_LINK") or os.getenv("GROUP_USERNAME", "")
WHATSAPP_LINK = os.getenv("WHATSAPP_LINK", "").strip()
FIREBASE_URL = os.getenv("FIREBASE_URL", "").strip()
IA_CAFE_API_KEY = os.getenv("IA_CAFE_API_KEY", "").strip()
ADMIN_CODES = [c.strip() for c in os.getenv("ADMIN_CODES", "").split(",") if c.strip()]

# ──────────────────────────────────────────────
# FIREBASE INITIALIZATION
# ──────────────────────────────────────────────
if not firebase_admin._apps:
    try:
        firebase_raw = os.getenv("FIREBASE_CREDENTIALS", "{}")
        firebase_clean = firebase_raw.encode().decode("unicode_escape")
        cred_data = json.loads(firebase_clean)
        cred = credentials.Certificate(cred_data)
        firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_URL})
        logger.info("✅ Firebase Database connected.")
    except Exception as e:
        logger.error(f"❌ Firebase init failed: {e}")

# ──────────────────────────────────────────────
# CONSTANTS & REWARD RULES
# ──────────────────────────────────────────────
SIGNUP_BONUS = 50
DAILY_BONUS = 10
MIN_WITHDRAW_AIRTIME = 350
DAILY_COOLDOWN_HOURS = 24

# Weighted referral pool: 30-50, majority receiving 35
REFERRAL_BONUS_POOL = [30, 35, 35, 35, 35, 35, 35, 40, 40, 45, 50]

# Network definitions & IA-Café Service IDs
NETWORK_PREFIXES = {
    "mtn": ["0703", "0706", "0803", "0806", "0810", "0813", "0814", "0816", "0903", "0906", "0913"],
    "airtel": ["0701", "0708", "0802", "0808", "0812", "0902", "0907", "0901", "0912"],
    "glo": ["0705", "0805", "0807", "0811", "0815", "0905", "0915"],
    "9mobile": ["0809", "0817", "0818", "0908", "0909"],
}

NETWORK_NAMES = {
    "mtn": "MTN",
    "airtel": "Airtel",
    "glo": "Glo",
    "9mobile": "9mobile"
}

NETWORK_MIN = {
    "mtn": 10,
    "airtel": 50,
    "glo": 50,
    "9mobile": 50
}

# Conversation States
(
    CHOOSING_TYPE,
    AIRTIME_PHONE,
    AIRTIME_AMOUNT,
    CONFIRM_AIRTIME,
    CASH_AMOUNT,
    CASH_BANK,
    CASH_ACCOUNT,
    CASH_NAME,
    CONFIRM_CASH
) = range(9)

# ──────────────────────────────────────────────
# DATABASE ACCESS HELPERS
# ──────────────────────────────────────────────
def get_user(user_id: str) -> dict:
    try:
        return db.reference(f"users/{user_id}").get() or {}
    except Exception as e:
        logger.error(f"Error reading user {user_id}: {e}")
        return {}

def save_user(user_id: str, data: dict):
    try:
        db.reference(f"users/{user_id}").update(data)
    except Exception as e:
        logger.error(f"Error saving user {user_id}: {e}")

def get_all_users() -> dict:
    try:
        return db.reference("users").get() or {}
    except Exception as e:
        logger.error(f"Error reading all users: {e}")
        return {}

def save_pending_withdrawal(req_id: str, data: dict):
    try:
        db.reference(f"pending_withdrawals/{req_id}").set(data)
    except Exception as e:
        logger.error(f"Error saving pending withdrawal: {e}")

def get_pending_withdrawals() -> dict:
    try:
        return db.reference("pending_withdrawals").get() or {}
    except Exception as e:
        logger.error(f"Error fetching pending withdrawals: {e}")
        return {}

def delete_pending_withdrawal(req_id: str):
    try:
        db.reference(f"pending_withdrawals/{req_id}").delete()
    except Exception as e:
        logger.error(f"Error deleting pending withdrawal {req_id}: {e}")

# ──────────────────────────────────────────────
# VALIDATION & UTILITIES
# ──────────────────────────────────────────────
def validate_nigerian_phone(phone_str: str) -> str | None:
    digits = re.sub(r"[^\d]", "", phone_str)
    if digits.startswith("234") and len(digits) == 13:
        digits = "0" + digits[3:]
    elif digits.startswith("+234") and len(digits) == 14:
        digits = "0" + digits[4:]

    if len(digits) == 11 and digits.startswith("0"):
        return digits
    return None

def detect_carrier(phone_11: str) -> str | None:
    prefix = phone_11[:4]
    for carrier, prefixes in NETWORK_PREFIXES.items():
        if prefix in prefixes:
            return carrier
    return None

def get_user_tier(referral_count: int) -> dict:
    if referral_count >= 100:
        return {"max_cash": 25000, "label": "💎 Diamond VIP (100+ Referrals)", "cash_eligible": True}
    elif referral_count >= 50:
        return {"max_cash": 10000, "label": "🥇 Gold Tier (50+ Referrals)", "cash_eligible": True}
    else:
        return {"max_cash": 0, "label": "🥉 Bronze Member (<50 Referrals)", "cash_eligible": False}

# ──────────────────────────────────────────────
# IA-CAFÉ AIRTIME API
# ──────────────────────────────────────────────
async def dispatch_airtime_api(phone: str, service_id: str, amount: int, user_id: str) -> dict:
    url = "https://iacafe.com.ng/devapi/v1/airtime"
    request_id = f"air_{service_id}_{int(time.time())}_{user_id}_{uuid.uuid4().hex[:5]}"
    
    headers = {
        "Authorization": f"Bearer {IA_CAFE_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    payload = {
        "request_id": request_id,
        "phone": phone,
        "service_id": service_id,
        "amount": amount
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            data = resp.json()
            logger.info(f"IA-Café API Response [{resp.status_code}]: {data}")
            return data
    except Exception as exc:
        logger.error(f"IA-Café Gateway Exception: {exc}")
        return {"code": "failed", "message": str(exc)}

# ──────────────────────────────────────────────
# CHANNEL & GROUP MEMBERSHIP CHECK
# ──────────────────────────────────────────────
async def verify_chat_membership(bot: Bot, user_id: int) -> bool:
    if not CHANNEL_ID:
        return True
    try:
        raw_id = CHANNEL_ID.strip()

        # Handle numeric ID (e.g. -1002345678901)
        if raw_id.startswith("-") and raw_id[1:].isdigit():
            chat_id = int(raw_id)
        elif raw_id.isdigit():
            chat_id = int(f"-100{raw_id}")
        else:
            # Clean username if provided
            cleaned = raw_id.replace("https://t.me/", "").replace("t.me/", "").strip("/")
            chat_id = cleaned if cleaned.startswith("@") else f"@{cleaned}"

        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        
        return member.status in [
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
            ChatMemberStatus.RESTRICTED
        ]
    except Exception as e:
        logger.warning(f"Chat check error for user {user_id} on {CHANNEL_ID}: {e}")
        return False

def get_join_link() -> str:
    link = CHANNEL_INVITE_LINK.strip()
    if link.startswith("http"):
        return link
    clean = link.lstrip("@")
    return f"https://t.me/{clean}"

# ──────────────────────────────────────────────
# UI KEYBOARDS
# ──────────────────────────────────────────────
def build_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Balance", callback_data="btn_balance"),
            InlineKeyboardButton("🔗 Referral Link", callback_data="btn_refer")
        ],
        [
            InlineKeyboardButton("💸 Withdraw", callback_data="btn_withdraw"),
            InlineKeyboardButton("🎁 Daily Check-in", callback_data="btn_daily")
        ],
        [
            InlineKeyboardButton("📜 History", callback_data="btn_history"),
            InlineKeyboardButton("ℹ️ Rules & Help", callback_data="btn_help")
        ]
    ])

def build_join_gate_keyboard(ref_code: str = "") -> InlineKeyboardMarkup:
    join_url = get_join_link()
    callback_param = f"checkjoin_{ref_code}" if ref_code else "checkjoin_"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Official Channel", url=join_url)],
        [InlineKeyboardButton("✅ I Have Joined", callback_data=callback_param)]
    ])

# ──────────────────────────────────────────────
# COMMAND & EVENT HANDLERS
# ──────────────────────────────────────────────
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    username = user.first_name or "Participant"
    ref_code = context.args[0] if context.args else None

    user_record = get_user(user_id)
    
    if user_record:
        await update.message.reply_text(
            f"👋 Welcome back, *{username}*!\n\nUse the buttons below to navigate:",
            reply_markup=build_main_keyboard(),
            parse_mode="Markdown"
        )
        return

    # Check Channel Membership
    is_member = await verify_chat_membership(context.bot, user.id)
    if not is_member:
        await update.message.reply_text(
            f"⚠️ *Channel Verification Required*\n\n"
            f"You must join our official Telegram channel to activate your account!\n\n"
            f"1. Click *'Join Official Channel'* below.\n"
            f"2. After joining, click *'I Have Joined'* to get your ₦{SIGNUP_BONUS} bonus.",
            reply_markup=build_join_gate_keyboard(ref_code or ""),
            parse_mode="Markdown"
        )
        return

    # Register user directly if already a member
    register_new_user(user_id, username, ref_code, context)
    await update.message.reply_text(
        f"🎊 *Registration Complete!*\n\n"
        f"Welcome, *{username}*! You received your ₦{SIGNUP_BONUS} welcome bonus.\n\n"
        f"📱 *WhatsApp Channel:* {WHATSAPP_LINK}\n\n"
        f"Choose an option below to start earning:",
        reply_markup=build_main_keyboard(),
        parse_mode="Markdown"
    )

def register_new_user(user_id: str, username: str, ref_code: str, context: ContextTypes.DEFAULT_TYPE):
    # Referral Reward Logic
    if ref_code and ref_code != user_id:
        referrer_data = get_user(ref_code)
        if referrer_data and user_id not in referrer_data.get("referrals", []):
            awarded_bonus = random.choice(REFERRAL_BONUS_POOL)
            ref_list = referrer_data.get("referrals", [])
            ref_list.append(user_id)
            new_ref_balance = referrer_data.get("balance", 0) + awarded_bonus
            
            save_user(ref_code, {
                "balance": new_ref_balance,
                "referrals": ref_list
            })
            
            try:
                context.application.create_task(context.bot.send_message(
                    chat_id=int(ref_code),
                    text=f"🎉 *New Referral Joined!*\n\n"
                         f"🎁 Reward Earned: *₦{awarded_bonus}*\n"
                         f"💰 Total Balance: *₦{new_ref_balance:,}*",
                    parse_mode="Markdown"
                ))
            except Exception:
                pass

    new_profile = {
        "id": user_id,
        "username": username,
        "balance": SIGNUP_BONUS,
        "referrals": [],
        "withdrawals": [],
        "ref_by": ref_code or "",
        "last_checkin": "",
        "is_admin": False,
        "joined_date": datetime.now().isoformat()
    }
    save_user(user_id, new_profile)

# Handle "✅ I Have Joined" Callback Button
async def handle_check_joined_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    user_id = str(user.id)
    username = user.first_name or "Participant"

    # Extract referral code from callback data
    data = query.data or ""
    ref_code = data.replace("checkjoin_", "").strip()

    is_member = await verify_chat_membership(context.bot, user.id)
    if not is_member:
        await query.answer("❌ You have not joined the channel yet! Please join first.", show_alert=True)
        return

    await query.answer("✅ Membership verified!")

    user_record = get_user(user_id)
    if not user_record:
        register_new_user(user_id, username, ref_code, context)

    await query.edit_message_text(
        f"🎊 *Verification Successful!*\n\n"
        f"Welcome, *{username}*! You have received your ₦{SIGNUP_BONUS} bonus.\n\n"
        f"📱 *WhatsApp Channel:* {WHATSAPP_LINK}\n\n"
        f"Use the dashboard below:",
        reply_markup=build_main_keyboard(),
        parse_mode="Markdown"
    )

async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_message = (
        "📖 *AIRTIME & CASH DROP RULES*\n\n"
        "1. *Daily Bonus:* Claim ₦10 every 24 hours via /daily.\n"
        "2. *Referrals:* Earn ₦30 - ₦50 randomly per active friend.\n\n"
        "💸 *WITHDRAWAL TIERS:*\n"
        "• *Bronze (< 50 referrals):* Airtime only (Min ₦350).\n"
        "• *Gold (50+ referrals):* Unlocks Bank Cash transfers up to *₦10,000*.\n"
        "• *Diamond (100+ referrals):* Unlocks Bank Cash transfers up to *₦25,000*.\n\n"
        "⚡ Airtime is delivered instantly via IA-Café.\n"
        "🏦 Cash withdrawals are reviewed and credited directly to your bank account."
    )
    if update.message:
        await update.message.reply_text(help_message, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.reply_text(help_message, parse_mode="Markdown")

async def handle_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = get_user(user_id)
    if not user_data:
        return

    balance = user_data.get("balance", 0)
    referrals = len(user_data.get("referrals", []))
    tier = get_user_tier(referrals)

    msg = (
        f"💳 *YOUR ACCOUNT STATUS*\n\n"
        f"💰 Available Balance: *₦{balance:,}*\n"
        f"👥 Active Referrals: *{referrals}*\n"
        f"🎖️ Status Tier: *{tier['label']}*\n"
        f"🏦 Cash Withdrawal Limit: *₦{tier['max_cash']:,}*"
    )
    if update.message:
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=build_main_keyboard())
    elif update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode="Markdown", reply_markup=build_main_keyboard())

async def handle_refer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = get_user(user_id)
    if not user_data:
        return

    bot_tag = context.bot.username
    ref_link = f"https://t.me/{bot_tag}?start={user_id}"
    total_refs = len(user_data.get("referrals", []))
    tier = get_user_tier(total_refs)

    msg = (
        f"🔗 *YOUR EXCLUSIVE REFERRAL LINK*\n\n"
        f"`{ref_link}`\n\n"
        f"📈 *Total Invited:* {total_refs}\n"
        f"🎖️ *Current Rank:* {tier['label']}\n\n"
        f"🎯 *Milestones:*\n"
        f"• 50 invites ➔ Unlock ₦10,000 Bank Cash Withdrawal\n"
        f"• 100 invites ➔ Unlock ₦25,000 Bank Cash Withdrawal\n\n"
        f"Share your link and earn ₦30 - ₦50 per friend!"
    )
    if update.message:
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=build_main_keyboard())
    elif update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode="Markdown", reply_markup=build_main_keyboard())

async def handle_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = get_user(user_id)
    if not user_data:
        return

    last_checkin_str = user_data.get("last_checkin", "")
    now = datetime.now()

    if last_checkin_str:
        last_dt = datetime.fromisoformat(last_checkin_str)
        if now - last_dt < timedelta(hours=DAILY_COOLDOWN_HOURS):
            remaining = timedelta(hours=DAILY_COOLDOWN_HOURS) - (now - last_dt)
            hours_left = int(remaining.total_seconds() // 3600)
            mins_left = int((remaining.total_seconds() % 3600) // 60)
            wait_text = f"⏳ *Cooldown Active*\n\nYou already claimed today's bonus.\nReturn in *{hours_left}h {mins_left}m*."
            if update.message:
                await update.message.reply_text(wait_text, parse_mode="Markdown")
            elif update.callback_query:
                await update.callback_query.answer(f"Come back in {hours_left}h {mins_left}m", show_alert=True)
            return

    new_balance = user_data.get("balance", 0) + DAILY_BONUS
    save_user(user_id, {
        "balance": new_balance,
        "last_checkin": now.isoformat()
    })

    success_msg = f"🎁 *Daily Bonus Claimed!*\n\n+₦{DAILY_BONUS} has been added to your wallet.\n💰 New Balance: *₦{new_balance:,}*"
    if update.message:
        await update.message.reply_text(success_msg, parse_mode="Markdown", reply_markup=build_main_keyboard())
    elif update.callback_query:
        await update.callback_query.edit_message_text(success_msg, parse_mode="Markdown", reply_markup=build_main_keyboard())

async def handle_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = get_user(user_id)
    if not user_data:
        return

    withdrawals = user_data.get("withdrawals", [])
    if not withdrawals:
        msg = "📜 *Transaction Log*\n\nNo withdrawals requested yet."
    else:
        msg = "📜 *Recent Transactions (Last 8):*\n\n"
        for item in reversed(withdrawals[-8:]):
            kind = "📱 Airtime" if item.get("type") == "airtime" else "🏦 Cash"
            date_str = item.get("date", "N/A")[:10]
            msg += f"• {kind} | *₦{item.get('amount', 0):,}* | `{item.get('status')}` ({date_str})\n"

    if update.message:
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=build_main_keyboard())
    elif update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode="Markdown", reply_markup=build_main_keyboard())

# ──────────────────────────────────────────────
# WITHDRAWAL CONVERSATION (AIRTIME & CASH)
# ──────────────────────────────────────────────
async def conv_withdraw_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = get_user(user_id)
    if not user_data:
        return ConversationHandler.END

    balance = user_data.get("balance", 0)
    referrals = len(user_data.get("referrals", []))
    tier = get_user_tier(referrals)

    if balance < MIN_WITHDRAW_AIRTIME:
        msg = f"❌ *Insufficient Balance*\n\nMinimum payout is *₦{MIN_WITHDRAW_AIRTIME}*.\nYour balance: ₦{balance}"
        if update.message:
            await update.message.reply_text(msg, parse_mode="Markdown")
        elif update.callback_query:
            await update.callback_query.message.reply_text(msg, parse_mode="Markdown")
        return ConversationHandler.END

    buttons = [
        [InlineKeyboardButton("📱 Instant Airtime Recharge", callback_data="payout_airtime")]
    ]
    if tier["cash_eligible"]:
        buttons.append([InlineKeyboardButton(f"🏦 Bank Transfer (Max ₦{tier['max_cash']:,})", callback_data="payout_cash")])
    else:
        buttons.append([InlineKeyboardButton("🔒 Bank Transfer (Requires 50+ Referrals)", callback_data="payout_cash_locked")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="payout_cancel")])

    text = (
        f"💸 *WITHDRAWAL PORTAL*\n\n"
        f"💰 Available Funds: *₦{balance:,}*\n"
        f"🎖️ Status: *{tier['label']}*\n\n"
        f"Select your preferred payout method:"
    )

    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

    return CHOOSING_TYPE

async def conv_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data

    if choice == "payout_cancel":
        await query.edit_message_text("❌ Withdrawal cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    if choice == "payout_cash_locked":
        await query.edit_message_text(
            "🔒 *Cash Withdrawal Locked*\n\n"
            "You need at least *50 verified referrals* to unlock direct bank cash transfers.\n"
            "Share your /refer link to unlock this feature!",
            parse_mode="Markdown"
        )
        context.user_data.clear()
        return ConversationHandler.END

    if choice == "payout_airtime":
        context.user_data["payout_type"] = "airtime"
        await query.edit_message_text(
            "📱 *Direct Airtime Recharge*\n\n"
            "Enter the 11-digit phone number to recharge:\n"
            "Example: `08031234567`",
            parse_mode="Markdown"
        )
        return AIRTIME_PHONE

    if choice == "payout_cash":
        user_id = str(update.effective_user.id)
        user_data = get_user(user_id)
        tier = get_user_tier(len(user_data.get("referrals", [])))
        context.user_data["payout_type"] = "cash"
        context.user_data["tier_limit"] = tier["max_cash"]

        await query.edit_message_text(
            f"🏦 *Direct Bank Transfer*\n\n"
            f"Tier Limit: *₦{tier['max_cash']:,}*\n"
            f"Minimum: *₦{MIN_WITHDRAW_AIRTIME}*\n\n"
            f"Enter the amount in Naira to withdraw:",
            parse_mode="Markdown"
        )
        return CASH_AMOUNT

# ── Airtime Flow ──
async def conv_airtime_phone_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_phone = update.message.text.strip()
    valid_phone = validate_nigerian_phone(raw_phone)

    if not valid_phone:
        await update.message.reply_text("❌ Invalid phone number. Enter a valid 11-digit Nigerian number:")
        return AIRTIME_PHONE

    carrier = detect_carrier(valid_phone)
    if not carrier:
        await update.message.reply_text("❌ Network not recognized. Please enter a valid MTN, Airtel, Glo, or 9mobile number:")
        return AIRTIME_PHONE

    context.user_data["phone"] = valid_phone
    context.user_data["service_id"] = carrier
    min_amount = NETWORK_MIN.get(carrier, 50)

    await update.message.reply_text(
        f"📱 *Phone:* `{valid_phone}`\n"
        f"📶 *Network:* {NETWORK_NAMES[carrier]}\n\n"
        f"Enter the recharge amount (Min: ₦{min_amount}, Max: ₦50,000):",
        parse_mode="Markdown"
    )
    return AIRTIME_AMOUNT

async def conv_airtime_amount_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int(re.sub(r"[^\d]", "", update.message.text.strip()))
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid numeric amount:")
        return AIRTIME_AMOUNT

    carrier = context.user_data.get("service_id", "mtn")
    min_allowed = NETWORK_MIN.get(carrier, 50)

    if amount < min_allowed or amount > 50000:
        await update.message.reply_text(f"❌ Amount must be between ₦{min_allowed} and ₦50,000.")
        return AIRTIME_AMOUNT

    user_id = str(update.effective_user.id)
    user_data = get_user(user_id)
    if amount > user_data.get("balance", 0):
        await update.message.reply_text(f"❌ Insufficient balance. Your balance is ₦{user_data.get('balance', 0):,}.")
        return AIRTIME_AMOUNT

    context.user_data["amount"] = amount
    phone = context.user_data["phone"]

    buttons = [
        [InlineKeyboardButton("✅ Confirm & Dispatch", callback_data="confirm_airtime_yes")],
        [InlineKeyboardButton("❌ Cancel", callback_data="confirm_airtime_no")]
    ]

    await update.message.reply_text(
        f"📋 *Confirm Airtime Purchase*\n\n"
        f"📞 *Recipient:* `{phone}`\n"
        f"📶 *Network:* {NETWORK_NAMES[carrier]}\n"
        f"💰 *Amount:* ₦{amount:,}\n\n"
        f"Confirm to send airtime instantly:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )
    return CONFIRM_AIRTIME

async def conv_airtime_confirm_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data != "confirm_airtime_yes":
        await query.edit_message_text("❌ Airtime purchase cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    user_id = str(update.effective_user.id)
    phone = context.user_data.get("phone")
    carrier = context.user_data.get("service_id")
    amount = context.user_data.get("amount")

    user_data = get_user(user_id)
    current_balance = user_data.get("balance", 0)
    if amount > current_balance:
        await query.edit_message_text("❌ Balance changed. Transaction aborted.")
        context.user_data.clear()
        return ConversationHandler.END

    new_bal = current_balance - amount
    history_entry = {
        "type": "airtime",
        "amount": amount,
        "phone": phone,
        "network": NETWORK_NAMES.get(carrier, carrier),
        "status": "processing",
        "date": datetime.now().isoformat()
    }
    withdrawals = user_data.get("withdrawals", [])
    withdrawals.append(history_entry)
    save_user(user_id, {"balance": new_bal, "withdrawals": withdrawals})

    await query.edit_message_text("⚙️ Contacting IA-Café Gateway... Please wait.")

    api_result = await dispatch_airtime_api(phone, carrier, amount, user_id)

    if api_result.get("code") == "success":
        withdrawals[-1]["status"] = "completed"
        withdrawals[-1]["order_id"] = api_result.get("data", {}).get("order_id", "")
        save_user(user_id, {"withdrawals": withdrawals})

        await context.bot.send_message(
            chat_id=int(user_id),
            text=f"✅ *Airtime Delivered!*\n\n"
                 f"📱 Phone: `{phone}` ({NETWORK_NAMES[carrier]})\n"
                 f"💰 Amount: *₦{amount:,}*\n"
                 f"💵 Balance: *₦{new_bal:,}*",
            parse_mode="Markdown",
            reply_markup=build_main_keyboard()
        )
    else:
        error_detail = api_result.get("message", "API Gateway Error")
        if isinstance(api_result.get("error"), dict):
            error_detail = api_result["error"].get("message", error_detail)

        refund_bal = new_bal + amount
        withdrawals[-1]["status"] = f"failed: {error_detail}"
        save_user(user_id, {"balance": refund_bal, "withdrawals": withdrawals})

        await context.bot.send_message(
            chat_id=int(user_id),
            text=f"❌ *Airtime Purchase Failed*\n\n"
                 f"Reason: `{error_detail}`\n"
                 f"💰 *₦{amount:,}* has been refunded to your wallet.\n"
                 f"💵 Balance: *₦{refund_bal:,}*",
            parse_mode="Markdown",
            reply_markup=build_main_keyboard()
        )

    context.user_data.clear()
    return ConversationHandler.END

# ── Cash Flow ──
async def conv_cash_amount_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int(re.sub(r"[^\d]", "", update.message.text.strip()))
    except ValueError:
        await update.message.reply_text("❌ Enter a valid numeric amount:")
        return CASH_AMOUNT

    user_id = str(update.effective_user.id)
    user_data = get_user(user_id)
    balance = user_data.get("balance", 0)
    tier_limit = context.user_data.get("tier_limit", 10000)

    if amount < MIN_WITHDRAW_AIRTIME:
        await update.message.reply_text(f"❌ Minimum withdrawal is ₦{MIN_WITHDRAW_AIRTIME}.")
        return CASH_AMOUNT

    if amount > tier_limit:
        await update.message.reply_text(f"❌ Amount exceeds your tier limit of ₦{tier_limit:,}.")
        return CASH_AMOUNT

    if amount > balance:
        await update.message.reply_text(f"❌ Insufficient balance. You have ₦{balance:,}.")
        return CASH_AMOUNT

    context.user_data["cash_amount"] = amount
    await update.message.reply_text("🏦 Enter your *Bank Name* (e.g. OPay, PalmPay, GTBank, Zenith):", parse_mode="Markdown")
    return CASH_BANK

async def conv_cash_bank_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bank_name = update.message.text.strip()
    if len(bank_name) < 2:
        await update.message.reply_text("❌ Please enter a valid bank name:")
        return CASH_BANK

    context.user_data["cash_bank"] = bank_name
    await update.message.reply_text("🔢 Enter your *10-digit Account Number*:", parse_mode="Markdown")
    return CASH_ACCOUNT

async def conv_cash_account_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    account_num = re.sub(r"[^\d]", "", update.message.text.strip())
    if len(account_num) != 10:
        await update.message.reply_text("❌ Account number must be 10 digits:")
        return CASH_ACCOUNT

    context.user_data["cash_account"] = account_num
    await update.message.reply_text("👤 Enter your *Account Name* (as registered in your bank):", parse_mode="Markdown")
    return CASH_NAME

async def conv_cash_name_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    acc_name = update.message.text.strip()
    if len(acc_name) < 3:
        await update.message.reply_text("❌ Enter full account name:")
        return CASH_NAME

    context.user_data["cash_name"] = acc_name
    amount = context.user_data["cash_amount"]
    bank = context.user_data["cash_bank"]
    account = context.user_data["cash_account"]

    buttons = [
        [InlineKeyboardButton("✅ Confirm & Submit", callback_data="confirm_cash_yes")],
        [InlineKeyboardButton("❌ Cancel", callback_data="confirm_cash_no")]
    ]

    await update.message.reply_text(
        f"📋 *Confirm Cash Withdrawal*\n\n"
        f"💰 *Amount:* ₦{amount:,}\n"
        f"🏦 *Bank:* {bank}\n"
        f"🔢 *Account:* `{account}`\n"
        f"👤 *Name:* {acc_name}\n\n"
        f"⚠️ Cash payouts will be reviewed and processed by admin.",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )
    return CONFIRM_CASH

async def conv_cash_confirm_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data != "confirm_cash_yes":
        await query.edit_message_text("❌ Cash withdrawal cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    user_id = str(update.effective_user.id)
    amount = context.user_data.get("cash_amount")
    bank = context.user_data.get("cash_bank")
    account = context.user_data.get("cash_account")
    name = context.user_data.get("cash_name")

    user_data = get_user(user_id)
    balance = user_data.get("balance", 0)

    if amount > balance:
        await query.edit_message_text("❌ Insufficient balance.")
        context.user_data.clear()
        return ConversationHandler.END

    new_bal = balance - amount
    req_id = f"c_{user_id}_{int(time.time())}"

    record = {
        "type": "cash",
        "amount": amount,
        "bank": bank,
        "account": account,
        "account_name": name,
        "status": "pending_approval",
        "req_id": req_id,
        "date": datetime.now().isoformat()
    }
    withdrawals = user_data.get("withdrawals", [])
    withdrawals.append(record)
    save_user(user_id, {"balance": new_bal, "withdrawals": withdrawals})

    save_pending_withdrawal(req_id, {
        "user_id": user_id,
        "username": user_data.get("username", "User"),
        "amount": amount,
        "bank": bank,
        "account": account,
        "account_name": name,
        "req_id": req_id,
        "date": datetime.now().isoformat()
    })

    # Notify all admins
    all_users = get_all_users()
    for uid, udata in all_users.items():
        if udata.get("is_admin"):
            try:
                await context.bot.send_message(
                    chat_id=int(uid),
                    text=f"🚨 *NEW CASH WITHDRAWAL REQUEST*\n\n"
                         f"👤 *User:* {user_data.get('username')} (`{user_id}`)\n"
                         f"💰 *Amount:* ₦{amount:,}\n"
                         f"🏦 *Bank:* {bank}\n"
                         f"🔢 *Account:* `{account}`\n"
                         f"👤 *Name:* {name}\n"
                         f"🆔 *ID:* `{req_id}`\n\n"
                         f"Actions:\n`/approve {req_id}`\n`/reject {req_id}`",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

    await query.edit_message_text(
        f"✅ *Request Submitted!*\n\n"
        f"💰 Amount: *₦{amount:,}*\n"
        f"🏦 Bank: {bank} - `{account}`\n"
        f"💵 Balance: *₦{new_bal:,}*\n\n"
        f"Your request has been sent to admin for approval.",
        parse_mode="Markdown"
    )
    context.user_data.clear()
    return ConversationHandler.END

async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Withdrawal process cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ──────────────────────────────────────────────
# ADMIN COMMANDS
# ──────────────────────────────────────────────
async def handle_admin_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    provided_code = context.args[0] if context.args else ""

    if provided_code in ADMIN_CODES and provided_code != "":
        save_user(user_id, {"is_admin": True})
        await update.message.reply_text(
            "🛡️ *Admin Access Granted!*\n\n"
            "Admin Commands:\n"
            "• `/pending` — List pending cash requests\n"
            "• `/approve <id>` — Approve withdrawal\n"
            "• `/reject <id>` — Reject and refund\n"
            "• `/stats` — Live metrics\n"
            "• `/broadcast <msg>` — Send to all users",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Invalid admin code.")

async def handle_admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not get_user(user_id).get("is_admin"):
        return

    pending = get_pending_withdrawals()
    if not pending:
        await update.message.reply_text("✅ No cash withdrawals currently pending.")
        return

    msg = "📋 *Pending Cash Withdrawals:*\n\n"
    for req_id, data in pending.items():
        msg += (
            f"🆔 `{req_id}`\n"
            f"👤 {data.get('username')} (`{data.get('user_id')}`)\n"
            f"💰 *₦{data.get('amount', 0):,}*\n"
            f"🏦 {data.get('bank')} | `{data.get('account')}`\n"
            f"👤 {data.get('account_name')}\n\n"
        )
    msg += "To approve: `/approve <id>`\nTo reject: `/reject <id>`"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_admin_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not get_user(user_id).get("is_admin"):
        return

    req_id = context.args[0] if context.args else ""
    pending = get_pending_withdrawals()

    if req_id not in pending:
        await update.message.reply_text("❌ Request ID not found in pending list.")
        return

    item = pending[req_id]
    target_uid = item["user_id"]
    target_user = get_user(target_uid)

    withdrawals = target_user.get("withdrawals", [])
    for w in withdrawals:
        if w.get("req_id") == req_id:
            w["status"] = "paid_approved"
            break
    save_user(target_uid, {"withdrawals": withdrawals})
    delete_pending_withdrawal(req_id)

    try:
        await context.bot.send_message(
            chat_id=int(target_uid),
            text=f"🎉 *Cash Withdrawal Approved!*\n\n"
                 f"💰 Amount: *₦{item['amount']:,}*\n"
                 f"🏦 Account: {item['bank']} (`{item['account']}`)\n\n"
                 f"Funds have been transferred to your bank account.",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    await update.message.reply_text(f"✅ Approved payout `{req_id}` for ₦{item['amount']:,}.")

async def handle_admin_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not get_user(user_id).get("is_admin"):
        return

    req_id = context.args[0] if context.args else ""
    pending = get_pending_withdrawals()

    if req_id not in pending:
        await update.message.reply_text("❌ Request ID not found.")
        return

    item = pending[req_id]
    target_uid = item["user_id"]
    amount = item["amount"]
    target_user = get_user(target_uid)

    new_bal = target_user.get("balance", 0) + amount
    withdrawals = target_user.get("withdrawals", [])
    for w in withdrawals:
        if w.get("req_id") == req_id:
            w["status"] = "rejected_refunded"
            break

    save_user(target_uid, {"balance": new_bal, "withdrawals": withdrawals})
    delete_pending_withdrawal(req_id)

    try:
        await context.bot.send_message(
            chat_id=int(target_uid),
            text=f"❌ *Cash Withdrawal Declined*\n\n"
                 f"💰 *₦{amount:,}* has been refunded to your wallet.\n"
                 f"💵 Balance: *₦{new_bal:,}*",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    await update.message.reply_text(f"❌ Rejected `{req_id}`. ₦{amount:,} refunded.")

async def handle_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not get_user(user_id).get("is_admin"):
        return

    all_users = get_all_users()
    total_users = len(all_users)
    total_wallet = sum(u.get("balance", 0) for u in all_users.values())
    total_refs = sum(len(u.get("referrals", [])) for u in all_users.values())
    pending_count = len(get_pending_withdrawals())

    await update.message.reply_text(
        f"📊 *BOT PERFORMANCE METRICS*\n\n"
        f"👥 Total Users: *{total_users:,}*\n"
        f"💰 Total Wallet Balances: *₦{total_wallet:,}*\n"
        f"🔗 Total Referrals: *{total_refs:,}*\n"
        f"⏳ Pending Cash Requests: *{pending_count}*",
        parse_mode="Markdown"
    )

async def handle_admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not get_user(user_id).get("is_admin"):
        return

    msg = " ".join(context.args) if context.args else ""
    if not msg:
        await update.message.reply_text("Usage: `/broadcast <message>`", parse_mode="Markdown")
        return

    all_users = get_all_users()
    sent_count, fail_count = 0, 0

    for uid in all_users:
        try:
            await context.bot.send_message(chat_id=int(uid), text=msg, parse_mode="Markdown")
            sent_count += 1
        except Exception:
            fail_count += 1

    await update.message.reply_text(
        f"📢 *Broadcast Complete:*\n✅ Delivered: {sent_count}\n❌ Failed: {fail_count}"
    )

# ──────────────────────────────────────────────
# MENU ROUTER
# ──────────────────────────────────────────────
async def handle_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    route = query.data

    if route == "btn_balance":
        await handle_balance(update, context)
    elif route == "btn_refer":
        await handle_refer(update, context)
    elif route == "btn_daily":
        await handle_daily(update, context)
    elif route == "btn_history":
        await handle_history(update, context)
    elif route == "btn_help":
        await handle_help(update, context)
    elif route == "btn_withdraw":
        await query.message.reply_text("💸 Type /withdraw to initiate withdrawal.")

# ──────────────────────────────────────────────
# MAIN ENTRYPOINT
# ──────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN is not set in environment variables!")
        return

    logger.info("🚀 Building Telegram Application...")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .rate_limiter(AIORateLimiter())
        .build()
    )

    withdraw_dialog = ConversationHandler(
        entry_points=[
            CommandHandler("withdraw", conv_withdraw_entry),
            CallbackQueryHandler(conv_withdraw_entry, pattern="^btn_withdraw$")
        ],
        states={
            CHOOSING_TYPE: [CallbackQueryHandler(conv_type_selected, pattern="^payout_")],
            AIRTIME_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_airtime_phone_step)],
            AIRTIME_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_airtime_amount_step)],
            CONFIRM_AIRTIME: [CallbackQueryHandler(conv_airtime_confirm_step, pattern="^confirm_airtime_")],
            CASH_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_cash_amount_step)],
            CASH_BANK: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_cash_bank_step)],
            CASH_ACCOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_cash_account_step)],
            CASH_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_cash_name_step)],
            CONFIRM_CASH: [CallbackQueryHandler(conv_cash_confirm_step, pattern="^confirm_cash_")]
        },
        fallbacks=[CommandHandler("cancel", conv_cancel)],
        conversation_timeout=300
    )

    # Register Handlers
    app.add_handler(CallbackQueryHandler(handle_check_joined_callback, pattern="^checkjoin_"))
    app.add_handler(withdraw_dialog)
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("balance", handle_balance))
    app.add_handler(CommandHandler("refer", handle_refer))
    app.add_handler(CommandHandler("daily", handle_daily))
    app.add_handler(CommandHandler("history", handle_history))
    app.add_handler(CommandHandler("help", handle_help))
    app.add_handler(CommandHandler("admin", handle_admin_auth))
    app.add_handler(CommandHandler("pending", handle_admin_pending))
    app.add_handler(CommandHandler("approve", handle_admin_approve))
    app.add_handler(CommandHandler("reject", handle_admin_reject))
    app.add_handler(CommandHandler("stats", handle_admin_stats))
    app.add_handler(CommandHandler("broadcast", handle_admin_broadcast))
    app.add_handler(CallbackQueryHandler(handle_menu_router, pattern="^btn_"))

    logger.info("✅ Airtime Drop Bot is running with polling...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

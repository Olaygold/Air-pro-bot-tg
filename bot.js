require('dotenv').config();
const express = require('express');
const { Telegraf, session } = require('telegraf');
const admin = require('firebase-admin');

// Initialize Firebase
const serviceAccount = JSON.parse(process.env.FIREBASE_CREDENTIALS);
admin.initializeApp({
  credential: admin.credential.cert(serviceAccount),
  databaseURL: process.env.FIREBASE_DATABASE_URL
});
const db = admin.database();

// Init Express & Telegraf
const app = express();
const bot = new Telegraf(process.env.BOT_TOKEN);

// Session middleware
bot.use(session());

app.use(bot.webhookCallback('/webhook'));
bot.telegram.setWebhook(`${process.env.WEBHOOK_URL}/webhook`);

// Constants
const SIGNUP_BONUS = 50;
const REFERRAL_BONUS = 50;
const MIN_WITHDRAW = 350;
const GROUP_USERNAME = process.env.GROUP_USERNAME;
const WHATSAPP_LINK = process.env.WHATSAPP_LINK;

// Firebase helpers
function userRef(userId) {
  return db.ref(`users/${userId}`);
}

async function getUser(userId) {
  const snap = await userRef(userId).once('value');
  return snap.exists() ? snap.val() : null;
}

async function saveUser(userId, data) {
  await userRef(userId).update(data);
}

// Check if user joined group
async function hasJoinedGroup(ctx) {
  try {
    const member = await ctx.telegram.getChatMember(GROUP_USERNAME, ctx.from.id);
    return ['member', 'administrator', 'creator'].includes(member.status);
  } catch (e) {
    return true; // Assume joined if private or inaccessible group
  }
}

// Start command with referral and group check
bot.start(async (ctx) => {
  const userId = ctx.from.id.toString();
  const username = ctx.from.first_name;
  const refCode = ctx.message.text.split(' ')[1];
  const existing = await getUser(userId);

  if (existing) {
    return ctx.reply('✅ You are already registered.');
  }

  const joined = await hasJoinedGroup(ctx);
  if (!joined) {
    return ctx.reply(`❌ Please join our group first: https://t.me/${GROUP_USERNAME.replace('@', '')}`);
  }

  await saveUser(userId, {
    id: userId,
    username,
    balance: SIGNUP_BONUS,
    referrals: [],
    withdrawals: [],
    ref_by: refCode || ''
  });

  if (refCode && refCode !== userId) {
    const refUser = await getUser(refCode);
    if (refUser && !refUser.referrals.includes(userId)) {
      refUser.balance += REFERRAL_BONUS;
      refUser.referrals.push(userId);
      await saveUser(refCode, refUser);
    }
  }

  await ctx.reply(`🎉 Welcome ${username}! You’ve received ₦${SIGNUP_BONUS} signup bonus.\n\n` +
    `👥 Join Telegram Group: https://t.me/${GROUP_USERNAME.replace('@', '')}\n` +
    `📱 WhatsApp Group (optional): ${WHATSAPP_LINK}`);
});

// Balance command
bot.command('balance', async (ctx) => {
  const user = await getUser(ctx.from.id);
  const bal = user?.balance || 0;
  ctx.reply(`💰 Your current balance is ₦${bal}`);
});

// Referral link
bot.command('refer', async (ctx) => {
  const botInfo = await bot.telegram.getMe();
  const link = `https://t.me/${botInfo.username}?start=${ctx.from.id}`;
  ctx.reply(`🔗 Your referral link:\n${link}`);
});

// Withdrawal history
bot.command('history', async (ctx) => {
  const user = await getUser(ctx.from.id);
  const referrals = user?.referrals || [];
  const withdrawals = user?.withdrawals || [];
  let text = `👥 Referrals: ${referrals.length}\n📜 Withdrawal History:\n`;

  if (withdrawals.length === 0) {
    text += '❌ No withdrawals yet.';
  } else {
    withdrawals.forEach(w => {
      text += `• ₦${w.amount} to ${w.phone} (${w.network}) - ${w.status}\n`;
    });
  }

  ctx.reply(text);
});

// Withdraw command
bot.command('withdraw', async (ctx) => {
  const user = await getUser(ctx.from.id);
  if (user.balance < MIN_WITHDRAW) {
    return ctx.reply(`❌ You need at least ₦${MIN_WITHDRAW} to withdraw.`);
  }

  ctx.session.withdraw = { step: 'phone' };
  ctx.reply('📱 Please enter your phone number for airtime:');
});

// Handle responses for withdraw steps
bot.on('text', async (ctx) => {
  const userId = ctx.from.id.toString();
  if (!ctx.session.withdraw) return;

  const step = ctx.session.withdraw.step;

  if (step === 'phone') {
    ctx.session.withdraw.phone = ctx.message.text;
    ctx.session.withdraw.step = 'network';
    return ctx.reply('📶 Enter your network (MTN, Airtel, Glo, 9mobile):');
  }

  if (step === 'network') {
    const phone = ctx.session.withdraw.phone;
    const network = ctx.message.text;
    const amount = MIN_WITHDRAW;

    const user = await getUser(userId);
    const withdrawals = user.withdrawals || [];

    withdrawals.push({ amount, phone, network, status: 'pending' });

    await saveUser(userId, {
      balance: user.balance - amount,
      withdrawals
    });

    ctx.session.withdraw = null;
    return ctx.reply(`✅ Withdrawal request of ₦${amount} submitted!\n📱 Airtime will be sent to ${phone} (${network})`);
  }
});

// Root route
app.get('/', (req, res) => res.send('✅ Airtime bot is running.'));

// Start server
app.listen(process.env.PORT || 3000, () => console.log('🚀 Bot is live on Express.'));

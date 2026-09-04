import os
import time
import json
import logging
import threading
import uuid
import gzip
import requests
import pyotp
from flask import Flask
from datetime import datetime
import openpyxl
from openpyxl.styles import Font, Alignment
from io import BytesIO

# ================== CONFIGURATION ==================
BOT_TOKEN = "8633623562:AAGhkUcUDeCSHqGQ9HSJ2HFpoeXZi8XKNgo"
ADMIN_CHAT_ID = "8538304896"
CHANNEL_ID = "-1003903695158"

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================== FILE PATHS ==================
USERS_FILE = "users.json"
SESSIONS_FILE = "sessions.json"
CREDENTIALS_FILE = "credentials.json"
WITHDRAWS_FILE = "withdraws.json"
CONFIG_FILE = "config.json"
USER_BALANCES_FILE = "user_balances.json"
USER_INFO_FILE = "user_info.json"
LANGUAGE_FILE = "language.json"
CREATED_ACCOUNTS_FILE = "created_accounts.json"
CANCEL_TRACKING_FILE = "cancel_tracking.json"

# ================== GLOBALS ==================
subscribed_users = set()
user_sessions = {}
credentials = []
withdraw_requests = []
created_accounts = []  # status: pending/approved/rejected
cancel_tracking = {}
config = {
    "base_balance": 10.0,
    "channel_id": CHANNEL_ID,
    "maintenance_mode": False,
    "work_rules_en": "📱 **Instagram Account Opening Guidelines**\n\n"
                     "1️⃣ You will receive an email and password.\n"
                     "2️⃣ Enter your Instagram username.\n"
                     "3️⃣ Enter 2FA code if required.\n"
                     "4️⃣ Follow 5 profiles and set profile picture.\n"
                     "5️⃣ Upon completion, your account will be sent for admin approval.",
    "work_rules_bn": "📱 **Instagram অ্যাকাউন্ট খোলার নিয়মাবলী**\n\n"
                     "1️⃣ আপনি একটি ইমেইল ও পাসওয়ার্ড পাবেন।\n"
                     "2️⃣ আপনার Instagram ইউজারনেম দিন।\n"
                     "3️⃣ 2FA কোড দিন (যদি থাকে)।\n"
                     "4️⃣ ৫টি প্রোফাইল ফলো করুন ও প্রোফাইল পিক সেট করুন।\n"
                     "5️⃣ সম্পন্ন হলে আপনার অ্যাকাউন্ট অ্যাডমিন অনুমোদনের জন্য জমা হবে।"
}
user_balances = {}
user_info = {}
user_language = {}

admin_cred_upload_session = {}
last_backup_message_id = None
last_backup_part_ids = []
save_pending = False
save_timer = None

data_lock = threading.RLock()
backup_lock = threading.Lock()
app = Flask(__name__)

# ================== FILE I/O ==================
def load_json(filename, default):
    try:
        with open(filename, "r") as f:
            return json.load(f)
    except:
        return default

def save_json(filename, data):
    with data_lock:
        try:
            with open(filename, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Save failed {filename}: {e}")

def load_all():
    global subscribed_users, user_sessions, credentials, withdraw_requests, config, user_balances, user_info, user_language, created_accounts, cancel_tracking
    subscribed_users = set(load_json(USERS_FILE, []))
    user_sessions = load_json(SESSIONS_FILE, {})
    credentials = load_json(CREDENTIALS_FILE, [])
    withdraw_requests = load_json(WITHDRAWS_FILE, [])
    created_accounts = load_json(CREATED_ACCOUNTS_FILE, [])
    if not isinstance(created_accounts, list):
        created_accounts = []
    user_balances = load_json(USER_BALANCES_FILE, {})
    user_info = load_json(USER_INFO_FILE, {})
    user_language = load_json(LANGUAGE_FILE, {})
    cancel_tracking = load_json(CANCEL_TRACKING_FILE, {})

    cfg = load_json(CONFIG_FILE, {})
    for k in config:
        if k not in cfg:
            cfg[k] = config[k]
    config = cfg

    if not config.get("channel_id"):
        config["channel_id"] = CHANNEL_ID
        save_json(CONFIG_FILE, config)

def save_all():
    save_json(USERS_FILE, list(subscribed_users))
    save_json(SESSIONS_FILE, user_sessions)
    save_json(CREDENTIALS_FILE, credentials)
    save_json(WITHDRAWS_FILE, withdraw_requests)
    save_json(CREATED_ACCOUNTS_FILE, created_accounts)
    save_json(CONFIG_FILE, config)
    save_json(USER_BALANCES_FILE, user_balances)
    save_json(USER_INFO_FILE, user_info)
    save_json(LANGUAGE_FILE, user_language)
    save_json(CANCEL_TRACKING_FILE, cancel_tracking)
    trigger_backup()

# ================== DEBOUNCED BACKUP ==================
def trigger_backup():
    global save_pending, save_timer
    with data_lock:
        if not save_pending:
            save_pending = True
            if save_timer:
                save_timer.cancel()
            save_timer = threading.Timer(3.0, execute_backup)
            save_timer.daemon = True
            save_timer.start()

def execute_backup():
    global save_pending
    with data_lock:
        save_pending = False
    save_data_to_channel()

# ================== TELEGRAM HELPERS ==================
def send_message(text, chat_id, reply_markup=None, parse_mode="Markdown"):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        return requests.post(url, json=payload, timeout=3)
    except Exception as e:
        logger.error(f"Send error: {e}")
        return None

def send_document(file_bytes, filename, chat_id, caption=""):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    try:
        files = {'document': (filename, file_bytes, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')}
        return requests.post(url, data={"chat_id": chat_id, "caption": caption}, files=files, timeout=60)
    except Exception as e:
        logger.error(f"Document error: {e}")
        return None

def delete_message(chat_id, message_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "message_id": message_id}, timeout=5)
    except:
        pass

def answer_callback(cb_id, text=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": cb_id}
    if text:
        payload["text"] = text
    try:
        requests.post(url, json=payload, timeout=5)
    except:
        pass

def edit_message_text(chat_id, message_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        return requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Edit error: {e}")
        return None

# ================== LANGUAGE HELPERS ==================
def get_lang(user_id):
    return user_language.get(str(user_id), "en")

def set_lang(user_id, lang):
    user_language[str(user_id)] = lang
    save_json(LANGUAGE_FILE, user_language)

def t(key, user_id, **kwargs):
    lang = get_lang(user_id)
    translations = {
        "en": {
            "welcome": "🤖 **Instagram Account Opener Bot**\n\nI help you open Instagram accounts.\nUse the buttons below to start.",
            "balance": "💰 **Your Balance**\n\nBalance: `{balance:.2f}` BDT\nTotal Accounts Opened: {total}",
            "withdraw": "💸 **Withdraw**\n\nChoose your withdrawal method:",
            "enter_account": "📞 Enter your {method} account number:",
            "enter_amount": "💰 **Enter the amount** you want to withdraw:",
            "withdraw_submitted": "✅ **Withdraw request submitted!**\n🆔 **ID:** `{w_id}`\n💰 **Amount:** {amount} BDT\n📌 Status: ⏳ Pending",
            "insufficient": "❌ **Insufficient balance!**",
            "invalid_amount": "❌ **Enter a valid amount.**",
            "account_assigned": "✅ **Your account has been assigned:**\n\n📧 **Email:** `{email}`\n🔑 **Password:** `{password}`\n\nPlease tap the button below and enter your **Instagram username**:",
            "enter_username_prompt": "👤 **Please enter your actual Instagram username:**",
            "twofa_prompt": "🔐 **Please enter your 2FA secret key:**\n(e.g., JBSWY3DPEHPK3PXP)\nBot will auto-generate the code.",
            "twofa_verified": "✅ **2FA code auto-generated!**\n🔐 Code: `{code}`\n\nNow follow **5 profiles & set profile picture**. Have you done that?",
            "follow_yes": "⚠️ **Please follow 5 profiles & set profile picture** then press **Yes**.",
            "completed": "🎉 **Account opening completed!**\n\n👤 **Username:** `{username}`\n📧 **Email:** `{email}`\n🔑 **Password:** `{password}`\n🔐 **2FA Secret:** {twofa}\n\n⏳ **Your account is now pending admin approval. You will receive balance once approved.**",
            "no_accounts": "❌ **No available accounts!** Please contact admin.",
            "active_session": "⏳ You already have an active session. Please finish or cancel it.",
            "under_maintenance": "🔧 **Bot is under maintenance. Please try later.**",
            "cancelled": "❌ Cancelled.",
            "unknown": "❌ I didn't understand that. Please use the buttons below.",
            "work": "📱 **Instagram Work**\n\n{rules}\n\n👇 Press **Start** to begin.",
            "language_changed": "🌐 Language changed to English.",
            "admin_panel": "⚙️ **Admin Control Panel**",
            "add_accounts": "📧 **Enter Email List**\n\nOne per line:\n`user1@gmail.com`\n`user2@yahoo.com`\n\nType /cancel to abort.",
            "add_accounts_success": "✅ **{added} accounts added.** Total: {total}",
            "no_valid_emails": "❌ **No valid emails found.**",
            "enter_password": "🔑 **Enter the password** (same for all accounts):",
            "password_empty": "❌ **Password cannot be empty.**",
            "account_list": "📋 **Account List** (Page {page}/{total_pages})\n\n",
            "account_item": "`{email}` | `{password}` | {status}",
            "deleted_all": "🗑️ **{count} accounts deleted.**",
            "stats": "📊 **Statistics**\n\n👥 Users: {users}\n📦 Credentials: {total} (Used: {used})\n💸 Pending Withdraw: {pending}\n💰 Price per account: {price} BDT\n⏳ Pending Approvals: {pending_acc}",
            "no_pending": "📭 **No pending withdraw requests.**",
            "pending_withdraws": "📥 **Pending Withdraw Requests**\n\n",
            "pending_item": "🆔 `{id}`\n👤 User: `{user}`\n💰 Amount: `{amount}` BDT\n💳 Method: {method}\n📞 Account: `{account}`\n🕒 Time: {time}\n",
            "approve_success": "✅ **Withdraw {w_id} approved.**",
            "reject_success": "❌ **Withdraw {w_id} rejected.**",
            "not_found": "❌ {w_id} not found.",
            "price_set": "✅ Price per account set to **{price}** BDT.",
            "rules_updated": "✅ **Rules updated.**",
            "channel_set": "✅ Channel ID set to `{channel}`.",
            "backup_created": "✅ **Backup created!**",
            "restore_completed": "✅ **Restore completed!**",
            "backup_creating": "⏳ **Creating backup...**",
            "restoring": "⏳ **Restoring data...**",
            "pending_approvals_list": "⏳ **Pending Approvals** (Page {page}/{total_pages})\n\n",
            "pending_approval_item": "🆔 `{id}` | 👤 User: `{user}` | 👤 Username: `{username}` | 📧 {email} | 🕒 {time}\n",
            "no_pending_approvals": "📭 **No pending approvals.**",
            "upload_approved_prompt": "📤 **Upload Approved List**\n\nSend a text file (.txt) or type the list of usernames (one per line) that you want to approve.\nType /cancel to abort.",
            "upload_rejected_prompt": "📤 **Upload Rejected List**\n\nSend a text file (.txt) or type the list of usernames (one per line) that you want to reject.\nType /cancel to abort.",
            "upload_approved_summary": "✅ **Approved List Processing Complete**\n\n✅ Approved: {approved}\n❌ Not Found: {not_found}\n⚠️ Already Processed: {already}",
            "upload_rejected_summary": "❌ **Rejected List Processing Complete**\n\n❌ Rejected: {rejected}\n❌ Not Found: {not_found}\n⚠️ Already Processed: {already}",
            "upload_no_usernames": "⚠️ No valid usernames found in the list.",
            "clear_exported_success": "🗑️ **{count} exported records cleared successfully!**",
            "export_excel": "📥 **Export Excel**\n\nClick below to download all created accounts as Excel file.",
            "excel_exported": "✅ **Excel file exported!**",
            "banned_work": "⛔ You are temporarily banned from Instagram work for {hours}h {minutes}m. Please try again later.",
            "unbanned_user": "✅ User {user_id} has been unbanned.",
            "my_accounts": "📜 My Accounts",
            "my_accounts_list": "📜 **My Account History** (Page {page}/{total_pages})\n\n",
            "my_account_item": "👤 Username: `{username}`\n📧 Email: `{email}`\n🔑 Password: `{password}`\n🔐 2FA Secret: `{twofa}`\n📌 Status: {status}\n🕒 {time}\n",
            "no_my_accounts": "📭 You haven't created any accounts yet.",
            "support_message": "📞 **Support**\n\nFor any help, contact:\n@mahirlabib45",
            "user_list": "👥 **User List** (Page {page}/{total_pages})\n\n",
            "user_list_item": "🆔 `{id}`\n💰 Balance: {balance:.2f} BDT\n📦 Accounts: {total}\nStatus: {status}\n\n",
            "no_users": "No users found.",
            "banned_list": "🚫 **Banned Users** (Page {page}/{total_pages})\n\n",
            "banned_user_item": "🆔 `{id}`\n⏳ Ban Remaining: {ban_time}\n\n",
            "no_banned_users": "🚫 No banned users.",
            "ban_success": "✅ User {user_id} has been banned.",
            "unban_success": "✅ User {user_id} has been unbanned.",
            "broadcast_prompt": "📢 **Broadcast Message**\n\nSend the message you want to broadcast to all users.\nType /cancel to abort.",
            "broadcast_success": "✅ Broadcast sent successfully.",
            "broadcast_cancelled": "❌ Broadcast cancelled.",
        },
        "bn": {
            "welcome": "🤖 **Instagram অ্যাকাউন্ট খোলার বট**\n\nআমি আপনাকে ইনস্টাগ্রাম অ্যাকাউন্ট খুলতে সাহায্য করি।\nনিচের বাটন ব্যবহার করে শুরু করুন।",
            "balance": "💰 **আপনার ব্যালেন্স**\n\nব্যালেন্স: `{balance:.2f}` টাকা\nমোট অ্যাকাউন্ট খোলা: {total}",
            "withdraw": "💸 **উইথড্র করুন**\n\nআপনার টাকা উত্তোলনের মাধ্যম নির্বাচন করুন:",
            "enter_account": "📞 আপনার {method} অ্যাকাউন্ট নম্বর দিন:",
            "enter_amount": "💰 **কত টাকা উইথড্র করতে চান?**",
            "withdraw_submitted": "✅ **উইথড্র রিকোয়েস্ট জমা হয়েছে!**\n🆔 **আইডি:** `{w_id}`\n💰 **পরিমাণ:** {amount} টাকা\n📌 স্ট্যাটাস: ⏳ পেন্ডিং",
            "insufficient": "❌ **অপর্যাপ্ত ব্যালেন্স!**",
            "invalid_amount": "❌ **সঠিক টাকার পরিমাণ দিন।**",
            "account_assigned": "✅ **আপনার জন্য একটি অ্যাকাউন্ট বরাদ্দ করা হয়েছে:**\n\n📧 **ইমেইল:** `{email}`\n🔑 **পাসওয়ার্ড:** `{password}`\n\nনিচের বাটনে ক্লিক করে আপনার **Instagram ইউজারনেম** দিন:",
            "enter_username_prompt": "👤 **আপনার প্রকৃত Instagram ইউজারনেম দিন:**",
            "twofa_prompt": "🔐 **আপনার 2FA সিক্রেট কী দিন:**\n(যেমন: JBSWY3DPEHPK3PXP)\nবট নিজেই কোড জেনারেট করবে।",
            "twofa_verified": "✅ **2FA কোড অটো-জেনারেট করা হয়েছে!**\n🔐 কোড: `{code}`\n\nএখন **৫টি প্রোফাইল ফলো ও প্রোফাইল পিক সেট** করুন। সম্পন্ন করেছেন?",
            "follow_yes": "⚠️ **দয়া করে ৫টি প্রোফাইল ফলো ও প্রোফাইল পিক সেট করুন** এবং তারপর **হ্যাঁ** বাটন চাপুন।",
            "completed": "🎉 **অ্যাকাউন্ট খোলা সম্পন্ন হয়েছে!**\n\n👤 **ইউজারনেম:** `{username}`\n📧 **ইমেইল:** `{email}`\n🔑 **পাসওয়ার্ড:** `{password}`\n🔐 **2FA সিক্রেট:** {twofa}\n\n⏳ **আপনার অ্যাকাউন্টটি এখন অ্যাডমিন অনুমোদনের অপেক্ষায়। অনুমোদন পেলে ব্যালেন্স যোগ হবে।**",
            "no_accounts": "❌ **কোনো অব্যবহৃত অ্যাকাউন্ট নেই!** অ্যাডমিনের সাথে যোগাযোগ করুন।",
            "active_session": "⏳ **আপনার একটি চলমান সেশন আছে।** আগে সেটি শেষ করুন বা বাতিল করুন।",
            "under_maintenance": "🔧 **বট রক্ষণাবেক্ষণে রয়েছে। পরে চেষ্টা করুন।**",
            "cancelled": "❌ বাতিল করা হয়েছে।",
            "unknown": "❌ আমি বুঝতে পারিনি। দয়া করে নিচের বাটন ব্যবহার করুন।",
            "work": "📱 **Instagram Work**\n\n{rules}\n\n👇 **Start** বাটনে ক্লিক করুন।",
            "language_changed": "🌐 ভাষা পরিবর্তন করে বাংলা করা হয়েছে।",
            "admin_panel": "⚙️ **অ্যাডমিন কন্ট্রোল প্যানেল**",
            "add_accounts": "📧 **ইমেইল লিস্ট দিন**\n\nপ্রতি লাইনে একটি:\n`user1@gmail.com`\n`user2@yahoo.com`\n\nবাতিল করতে /cancel লিখুন।",
            "add_accounts_success": "✅ **{added} টি অ্যাকাউন্ট যোগ করা হয়েছে।** মোট: {total}",
            "no_valid_emails": "❌ **কোনো বৈধ ইমেইল পাওয়া যায়নি।**",
            "enter_password": "🔑 **পাসওয়ার্ড দিন** (সব অ্যাকাউন্টের জন্য একই):",
            "password_empty": "❌ **পাসওয়ার্ড খালি রাখা যাবে না।**",
            "account_list": "📋 **অ্যাকাউন্ট লিস্ট** (পৃষ্ঠা {page}/{total_pages})\n\n",
            "account_item": "`{email}` | `{password}` | {status}",
            "deleted_all": "🗑️ **{count} টি অ্যাকাউন্ট ডিলিট করা হয়েছে।**",
            "stats": "📊 **পরিসংখ্যান**\n\n👥 ইউজার: {users}\n📦 ক্রেডেনশিয়াল: {total} (ব্যবহৃত: {used})\n💸 পেন্ডিং উইথড্র: {pending}\n💰 প্রতি অ্যাকাউন্ট মূল্য: {price} টাকা\n⏳ পেন্ডিং অ্যাপ্রুভাল: {pending_acc}",
            "no_pending": "📭 **কোনো পেন্ডিং উইথড্র রিকোয়েস্ট নেই।**",
            "pending_withdraws": "📥 **পেন্ডিং উইথড্র রিকোয়েস্ট**\n\n",
            "pending_item": "🆔 `{id}`\n👤 ইউজার: `{user}`\n💰 পরিমাণ: `{amount}` টাকা\n💳 মাধ্যম: {method}\n📞 অ্যাকাউন্ট: `{account}`\n🕒 সময়: {time}\n",
            "approve_success": "✅ **উইথড্র {w_id} অনুমোদিত হয়েছে।**",
            "reject_success": "❌ **উইথড্র {w_id} বাতিল করা হয়েছে।**",
            "not_found": "❌ {w_id} পাওয়া যায়নি।",
            "price_set": "✅ প্রতি অ্যাকাউন্ট খোলার ইনকাম **{price}** টাকা সেট করা হয়েছে।",
            "rules_updated": "✅ **নিয়মাবলী আপডেট করা হয়েছে।**",
            "channel_set": "✅ চ্যানেল আইডি `{channel}` সেট করা হয়েছে।",
            "backup_created": "✅ **ব্যাকআপ তৈরি হয়েছে!**",
            "restore_completed": "✅ **রিস্টোর সম্পন্ন!**",
            "backup_creating": "⏳ **ব্যাকআপ নেওয়া হচ্ছে...**",
            "restoring": "⏳ **ডেটা রিস্টোর করা হচ্ছে...**",
            "pending_approvals_list": "⏳ **পেন্ডিং অ্যাপ্রুভাল** (পৃষ্ঠা {page}/{total_pages})\n\n",
            "pending_approval_item": "🆔 `{id}` | 👤 ইউজার: `{user}` | 👤 ইউজারনেম: `{username}` | 📧 {email} | 🕒 {time}\n",
            "no_pending_approvals": "📭 **কোনো পেন্ডিং অ্যাপ্রুভাল নেই।**",
            "upload_approved_prompt": "📤 **অ্যাপ্রুভড লিস্ট আপলোড**\n\nএকটি টেক্সট ফাইল (.txt) আপলোড করুন অথবা ইউজারনেমের তালিকা টাইপ করুন (প্রতি লাইনে একটি) যাদের অ্যাপ্রুভ করতে চান।\nবাতিল করতে /cancel লিখুন।",
            "upload_rejected_prompt": "📤 **রিজেক্টেড লিস্ট আপলোড**\n\nএকটি টেক্সট ফাইল (.txt) আপলোড করুন অথবা ইউজারনেমের তালিকা টাইপ করুন (প্রতি লাইনে একটি) যাদের রিজেক্ট করতে চান।\nবাতিল করতে /cancel লিখুন।",
            "upload_approved_summary": "✅ **অ্যাপ্রুভড লিস্ট প্রসেসিং সম্পন্ন**\n\n✅ অ্যাপ্রুভড: {approved}\n❌ পাওয়া যায়নি: {not_found}\n⚠️ ইতিমধ্যে প্রসেসড: {already}",
            "upload_rejected_summary": "❌ **রিজেক্টেড লিস্ট প্রসেসিং সম্পন্ন**\n\n❌ রিজেক্টেড: {rejected}\n❌ পাওয়া যায়নি: {not_found}\n⚠️ ইতিমধ্যে প্রসেসড: {already}",
            "upload_no_usernames": "⚠️ তালিকায় কোনো বৈধ ইউজারনেম পাওয়া যায়নি।",
            "clear_exported_success": "🗑️ **{count} টি এক্সপোর্ট করা রেকর্ড মুছে ফেলা হয়েছে!**",
            "export_excel": "📥 **এক্সেল এক্সপোর্ট**\n\nনিচের বাটনে ক্লিক করে সব অ্যাকাউন্টের Excel ফাইল ডাউনলোড করুন।",
            "excel_exported": "✅ **Excel ফাইল তৈরি হয়েছে!**",
            "banned_work": "⛔ আপনি Instagram কাজ থেকে {hours} ঘণ্টা {minutes} মিনিটের জন্য ব্যান হয়েছেন। পরে আবার চেষ্টা করুন।",
            "unbanned_user": "✅ ইউজার {user_id} কে আনব্যান করা হয়েছে।",
            "my_accounts": "📜 আমার অ্যাকাউন্ট",
            "my_accounts_list": "📜 **আমার অ্যাকাউন্ট ইতিহাস** (পৃষ্ঠা {page}/{total_pages})\n\n",
            "my_account_item": "👤 ইউজারনেম: `{username}`\n📧 ইমেইল: `{email}`\n🔑 পাসওয়ার্ড: `{password}`\n🔐 2FA সিক্রেট: `{twofa}`\n📌 স্ট্যাটাস: {status}\n🕒 {time}\n",
            "no_my_accounts": "📭 আপনি এখনো কোনো অ্যাকাউন্ট খোলেননি।",
            "support_message": "📞 **সাপোর্ট**\n\nসাহায্যের জন্য যোগাযোগ করুন:\n@mahirlabib45",
            "user_list": "👥 **ইউজার লিস্ট** (পৃষ্ঠা {page}/{total_pages})\n\n",
            "user_list_item": "🆔 `{id}`\n💰 ব্যালেন্স: {balance:.2f} টাকা\n📦 অ্যাকাউন্ট: {total}\nস্ট্যাটাস: {status}\n\n",
            "no_users": "কোনো ইউজার পাওয়া যায়নি।",
            "banned_list": "🚫 **ব্যানড ইউজার** (পৃষ্ঠা {page}/{total_pages})\n\n",
            "banned_user_item": "🆔 `{id}`\n⏳ ব্যান বাকি: {ban_time}\n\n",
            "no_banned_users": "🚫 কোনো ব্যানড ইউজার নেই।",
            "ban_success": "✅ ইউজার {user_id} কে ব্যান করা হয়েছে।",
            "unban_success": "✅ ইউজার {user_id} কে আনব্যান করা হয়েছে।",
            "broadcast_prompt": "📢 **ব্রডকাস্ট মেসেজ**\n\nসব ইউজারকে পাঠানোর জন্য মেসেজ লিখুন।\nবাতিল করতে /cancel লিখুন।",
            "broadcast_success": "✅ ব্রডকাস্ট সফলভাবে পাঠানো হয়েছে।",
            "broadcast_cancelled": "❌ ব্রডকাস্ট বাতিল করা হয়েছে।",
        }
    }
    text = translations.get(lang, translations["en"]).get(key, key)
    if kwargs:
        text = text.format(**kwargs)
    return text

# ================== BALANCE & USER FUNCTIONS ==================
def add_balance(user_id, amount):
    with data_lock:
        uid = str(user_id)
        user_balances[uid] = user_balances.get(uid, 0.0) + amount
        save_json(USER_BALANCES_FILE, user_balances)
    trigger_backup()

def deduct_balance(user_id, amount):
    with data_lock:
        uid = str(user_id)
        if user_balances.get(uid, 0.0) >= amount:
            user_balances[uid] -= amount
            save_json(USER_BALANCES_FILE, user_balances)
            trigger_backup()
            return True
        return False

def get_balance_text(user_id):
    uid = str(user_id)
    balance = user_balances.get(uid, 0.0)
    total_accounts = user_info.get(uid, {}).get("total_accounts", 0)
    return t("balance", user_id, balance=balance, total=total_accounts)

# ================== BAN HELPERS ==================
def record_cancel(user_id):
    uid = str(user_id)
    now = time.time()
    with data_lock:
        ct = cancel_tracking.setdefault(uid, {"consecutive": 0, "timestamps": [], "ban_end": 0})
        ct["consecutive"] += 1
        ct["timestamps"].append(now)
        ct["timestamps"] = [t for t in ct["timestamps"] if now - t <= 86400]
        if ct["consecutive"] >= 3 or len(ct["timestamps"]) >= 5:
            ct["ban_end"] = now + 86400
            ct["consecutive"] = 0
        save_json(CANCEL_TRACKING_FILE, cancel_tracking)

def reset_consecutive_cancels(user_id):
    uid = str(user_id)
    with data_lock:
        if uid in cancel_tracking:
            cancel_tracking[uid]["consecutive"] = 0
            save_json(CANCEL_TRACKING_FILE, cancel_tracking)

def is_banned(user_id):
    ct = cancel_tracking.get(str(user_id))
    if ct:
        if ct.get("manual_ban", False):
            return True, -1
        if ct.get("ban_end", 0) > time.time():
            return True, ct["ban_end"]
    return False, 0

def get_ban_remaining(user_id):
    ct = cancel_tracking.get(str(user_id))
    if ct:
        if ct.get("manual_ban", False):
            return -1
        if ct.get("ban_end", 0) > time.time():
            return int(ct["ban_end"] - time.time())
    return 0

def unban_user(user_id):
    uid = str(user_id)
    with data_lock:
        if uid in cancel_tracking:
            del cancel_tracking[uid]
            save_json(CANCEL_TRACKING_FILE, cancel_tracking)

def manual_ban_user(user_id):
    uid = str(user_id)
    with data_lock:
        ct = cancel_tracking.setdefault(uid, {"consecutive": 0, "timestamps": [], "ban_end": 0})
        ct["manual_ban"] = True
        ct["ban_end"] = 0
        save_json(CANCEL_TRACKING_FILE, cancel_tracking)

# ================== KEYBOARDS ==================
def main_keyboard(chat_id):
    lang = get_lang(chat_id)
    kb = [
        ["📱 Instagram Work", "💰 Balance"],
        ["💸 Withdraw", "🌐 Language"],
        ["📜 My Accounts", "📞 Support"]
    ]
    if str(chat_id) == ADMIN_CHAT_ID:
        kb.append(["⚙️ Admin Panel"])
    return {"keyboard": kb, "resize_keyboard": True}

def admin_keyboard():
    maint_status = "🟢 ON" if config.get("maintenance_mode", False) else "🔴 OFF"
    return {
        "keyboard": [
            ["➕ Add Accounts", "📋 Account List"],
            ["🗑️ Delete All", "📊 Statistics"],
            ["💲 Set Price", "📝 Edit Rules"],
            ["📥 Withdraw Requests", "📁 Backup"],
            ["📥 Restore", f"🔧 Maintenance {maint_status}"],
            ["📋 Pending Approvals", "📥 Export Excel"],
            ["📤 Upload Approved", "📤 Upload Rejected"],
            ["🗑️ Clear Exported Accounts", "🔙 Main Menu"],
            ["👥 User List", "🚫 Banned Users"],
            ["📢 Broadcast"]
        ],
        "resize_keyboard": True
    }

def cancel_keyboard(chat_id):
    lang = get_lang(chat_id)
    return {
        "keyboard": [
            ["❌ Cancel" if lang == "en" else "❌ বাতিল"]
        ],
        "resize_keyboard": True
    }

def yes_no_keyboard(chat_id):
    lang = get_lang(chat_id)
    return {
        "keyboard": [
            ["✅ Yes" if lang == "en" else "✅ হ্যাঁ", "❌ No" if lang == "en" else "❌ না"]
        ],
        "resize_keyboard": True
    }

def next_or_cancel_keyboard(chat_id):
    lang = get_lang(chat_id)
    return {
        "keyboard": [
            ["🔄 Open Another Account" if lang == "en" else "🔄 আরেকটি অ্যাকাউন্ট খুলুন"],
            ["❌ Cancel" if lang == "en" else "❌ বাতিল"]
        ],
        "resize_keyboard": True
    }

def withdraw_method_keyboard(chat_id):
    lang = get_lang(chat_id)
    return {
        "keyboard": [
            ["💸 bKash", "💸 Nagad"],
            ["❌ Cancel" if lang == "en" else "❌ বাতিল"]
        ],
        "resize_keyboard": True
    }

def work_start_keyboard(chat_id):
    lang = get_lang(chat_id)
    return {
        "keyboard": [
            ["🚀 Start" if lang == "en" else "🚀 শুরু করুন"],
            ["❌ Cancel" if lang == "en" else "❌ বাতিল"]
        ],
        "resize_keyboard": True
    }

def username_input_keyboard(chat_id):
    lang = get_lang(chat_id)
    return {
        "keyboard": [
            ["👤 Enter Username" if lang == "en" else "👤 ইউজারনেম দিন"],
            ["❌ Cancel" if lang == "en" else "❌ বাতিল"]
        ],
        "resize_keyboard": True
    }

# ================== CREDENTIALS MANAGEMENT ==================
def get_available_credential():
    with data_lock:
        for cred in credentials:
            if not cred.get("used", False):
                cred["used"] = True
                cred["assigned_to"] = None
                save_all()
                return cred
    return None

def assign_credential_to_user(cred, user_id):
    with data_lock:
        for c in credentials:
            if c["email"] == cred["email"] and c["password"] == cred["password"]:
                c["assigned_to"] = str(user_id)
                save_all()
                return True
    return False

def delete_credential_by_index(index):
    with data_lock:
        if 0 <= index < len(credentials):
            deleted = credentials.pop(index)
            save_all()
            return deleted
    return None

# ================== CREATED ACCOUNTS MANAGEMENT ==================
def save_created_account(username, email, password, twofa, user_id):
    with data_lock:
        acc_id = uuid.uuid4().hex[:10]
        created_accounts.append({
            "id": acc_id,
            "username": username,
            "email": email,
            "password": password,
            "twofa": twofa,
            "user_id": str(user_id),
            "timestamp": time.time(),
            "status": "pending"
        })
        save_json(CREATED_ACCOUNTS_FILE, created_accounts)
    trigger_backup()
    return acc_id

def approve_account(acc_id):
    with data_lock:
        for acc in created_accounts:
            if acc.get("id") == acc_id and acc.get("status") == "pending":
                acc["status"] = "approved"
                user_id = acc["user_id"]
                price = config.get("base_balance", 10.0)
                add_balance(user_id, price)
                uid = str(user_id)
                if uid in user_info:
                    user_info[uid]["total_accounts"] = user_info[uid].get("total_accounts", 0) + 1
                else:
                    user_info[uid] = {"name": f"User_{user_id}", "total_accounts": 1}
                save_json(USER_INFO_FILE, user_info)
                save_json(CREATED_ACCOUNTS_FILE, created_accounts)
                send_message(
                    f"✅ Your account (Username: {acc['username']}) has been approved! {price} BDT added to your balance.",
                    user_id
                )
                return True
    return False

def reject_account(acc_id):
    with data_lock:
        for acc in created_accounts:
            if acc.get("id") == acc_id and acc.get("status") == "pending":
                acc["status"] = "rejected"
                save_json(CREATED_ACCOUNTS_FILE, created_accounts)
                user_id = acc["user_id"]
                send_message(
                    f"❌ Your account (Username: {acc['username']}) has been rejected. Please contact admin for details.",
                    user_id
                )
                return True
    return False

# ================== BULK PROCESSING FROM LIST ==================
def process_approve_list(usernames):
    approved = 0
    not_found = 0
    already = 0
    price = config.get("base_balance", 10.0)
    with data_lock:
        for username in usernames:
            username = username.strip()
            if not username:
                continue
            found = False
            for acc in created_accounts:
                if acc.get("username") == username and acc.get("status") == "pending":
                    acc["status"] = "approved"
                    user_id = acc["user_id"]
                    add_balance(user_id, price)
                    uid = str(user_id)
                    if uid in user_info:
                        user_info[uid]["total_accounts"] = user_info[uid].get("total_accounts", 0) + 1
                    else:
                        user_info[uid] = {"name": f"User_{user_id}", "total_accounts": 1}
                    send_message(
                        f"✅ Your account (Username: {username}) has been approved! {price} BDT added to your balance.",
                        user_id
                    )
                    approved += 1
                    found = True
                    break
            if not found:
                already_exists = any(acc.get("username") == username and acc.get("status") != "pending" for acc in created_accounts)
                if already_exists:
                    already += 1
                else:
                    not_found += 1
        save_json(USER_INFO_FILE, user_info)
        save_json(CREATED_ACCOUNTS_FILE, created_accounts)
    return approved, not_found, already

def process_reject_list(usernames):
    rejected = 0
    not_found = 0
    already = 0
    with data_lock:
        for username in usernames:
            username = username.strip()
            if not username:
                continue
            found = False
            for acc in created_accounts:
                if acc.get("username") == username and acc.get("status") == "pending":
                    acc["status"] = "rejected"
                    user_id = acc["user_id"]
                    send_message(
                        f"❌ Your account (Username: {username}) has been rejected. Please contact admin.",
                        user_id
                    )
                    rejected += 1
                    found = True
                    break
            if not found:
                already_exists = any(acc.get("username") == username and acc.get("status") != "pending" for acc in created_accounts)
                if already_exists:
                    already += 1
                else:
                    not_found += 1
        save_json(CREATED_ACCOUNTS_FILE, created_accounts)
    return rejected, not_found, already

def parse_username_list(text):
    lines = text.strip().splitlines()
    usernames = [line.strip() for line in lines if line.strip()]
    return usernames

# ================== EXCEL GENERATION ==================
def generate_created_accounts_excel():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Created Accounts"
    headers = ["ID", "Username", "Email", "Password", "2FA Secret", "User ID", "Status", "Timestamp"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")
    for acc in created_accounts:
        time_str = datetime.fromtimestamp(acc["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
        ws.append([
            acc.get("id", "N/A"),
            acc.get("username", "N/A"),
            acc.get("email", "N/A"),
            acc.get("password", "N/A"),
            acc.get("twofa", "N/A"),
            acc.get("user_id", "N/A"),
            acc.get("status", "N/A"),
            time_str
        ])
    for col in ws.columns:
        max_length = 0
        for cell in col:
            try:
                if len(str(cell.value)) > max_length:
                    max_length = len(cell.value)
            except:
                pass
        ws.column_dimensions[col[0].column_letter].width = (max_length + 2) * 1.2
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output.read()

# ================== SESSION MANAGEMENT ==================
def get_session(chat_id):
    return user_sessions.get(str(chat_id))

def set_session(chat_id, data):
    user_sessions[str(chat_id)] = data
    save_all()

def clear_session(chat_id):
    user_sessions.pop(str(chat_id), None)
    save_all()

# ================== BACKUP SYSTEM ==================
MAX_PART_SIZE = 45 * 1024 * 1024

def cleanup_old_channel_backup():
    global last_backup_message_id, last_backup_part_ids
    channel_id = config.get("channel_id")
    if not channel_id:
        return
    try:
        if last_backup_message_id:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/unpinChatMessage",
                json={"chat_id": channel_id, "message_id": last_backup_message_id},
                timeout=10
            )
        for part_id in last_backup_part_ids:
            delete_message(channel_id, part_id)
        if last_backup_message_id:
            delete_message(channel_id, last_backup_message_id)
    except Exception as e:
        logger.error(f"Backup cleanup error: {e}")

def generate_backup_data():
    with data_lock:
        return {
            "subscribed_users": list(subscribed_users),
            "credentials": credentials,
            "withdraw_requests": withdraw_requests,
            "created_accounts": created_accounts,
            "config": config,
            "user_balances": user_balances,
            "user_info": user_info,
            "user_language": user_language,
            "cancel_tracking": cancel_tracking,
            "timestamp": datetime.now().isoformat()
        }

def save_data_to_channel():
    global last_backup_message_id, last_backup_part_ids
    channel_id = config.get("channel_id")
    if not channel_id:
        logger.warning("Backup channel ID not set!")
        return

    with backup_lock:
        try:
            data = generate_backup_data()
            json_bytes = json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')
            compressed = gzip.compress(json_bytes, compresslevel=6)

            new_backup_ids = []
            new_part_ids = []
            s = requests.Session()

            if len(compressed) <= MAX_PART_SIZE:
                filename = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json.gz"
                files = {'document': (filename, compressed, 'application/gzip')}
                resp = s.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                    data={"chat_id": channel_id},
                    files=files,
                    timeout=60
                )
                if resp.status_code == 200 and resp.json().get("ok"):
                    new_backup_ids = [resp.json()["result"]["message_id"]]
                else:
                    logger.error("Failed to send single backup")
                    return
            else:
                chunks = [compressed[i:i+MAX_PART_SIZE] for i in range(0, len(compressed), MAX_PART_SIZE)]
                part_ids = []
                total = len(chunks)
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                for idx, chunk in enumerate(chunks, 1):
                    part_filename = f"backup_{timestamp}_part{idx}of{total}.json.gz"
                    resp = s.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                        data={"chat_id": channel_id, "caption": f"Part {idx}/{total}"},
                        files={"document": (part_filename, chunk, "application/gzip")},
                        timeout=60
                    )
                    if resp.status_code == 200 and resp.json().get("ok"):
                        part_ids.append(resp.json()["result"]["message_id"])
                    else:
                        logger.error(f"Failed to send part {idx}")
                        return
                index_data = {
                    "backup_id": timestamp,
                    "parts": part_ids,
                    "total_parts": total,
                    "timestamp": timestamp
                }
                index_text = json.dumps(index_data)
                resp = s.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={"chat_id": channel_id, "text": index_text},
                    timeout=60
                )
                if resp.status_code == 200 and resp.json().get("ok"):
                    new_backup_ids = [resp.json()["result"]["message_id"]]
                    new_part_ids = part_ids
                else:
                    logger.error("Failed to send index message")
                    return

            cleanup_old_channel_backup()
            if new_backup_ids:
                s.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/pinChatMessage",
                    json={
                        "chat_id": channel_id,
                        "message_id": new_backup_ids[0],
                        "disable_notification": True
                    },
                    timeout=10
                )
                last_backup_message_id = new_backup_ids[0]
                last_backup_part_ids = new_part_ids
                logger.info("Backup successfully saved to channel")
        except Exception as e:
            logger.error(f"Backup error: {e}")

def manual_backup(chat_id):
    try:
        data = generate_backup_data()
        json_bytes = json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')
        compressed = gzip.compress(json_bytes, compresslevel=6)
        filename = f"backup_manual_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json.gz"
        resp = send_document(compressed, filename, chat_id, caption=t("backup_created", chat_id))
        return resp and resp.status_code == 200
    except Exception as e:
        logger.error(f"Manual backup error: {e}")
        return False

def manual_restore(chat_id, file_content):
    try:
        decompressed = gzip.decompress(file_content)
        data = json.loads(decompressed.decode('utf-8'))
        with data_lock:
            subscribed_users.clear()
            subscribed_users.update(data.get("subscribed_users", []))
            credentials.clear()
            credentials.extend(data.get("credentials", []))
            withdraw_requests.clear()
            withdraw_requests.extend(data.get("withdraw_requests", []))
            created_accounts.clear()
            created_accounts.extend(data.get("created_accounts", []))
            if "config" in data:
                for k in data["config"]:
                    config[k] = data["config"][k]
            user_balances.clear()
            user_balances.update(data.get("user_balances", {}))
            user_info.clear()
            user_info.update(data.get("user_info", {}))
            user_language.clear()
            user_language.update(data.get("user_language", {}))
            cancel_tracking.clear()
            cancel_tracking.update(data.get("cancel_tracking", {}))
            save_all()
        logger.info(f"Manual restore completed: {len(subscribed_users)} users, {len(credentials)} credentials, {len(created_accounts)} created accounts")
        return True
    except Exception as e:
        logger.error(f"Manual restore error: {e}")
        return False

def auto_restore_from_channel():
    global last_backup_message_id, last_backup_part_ids
    global subscribed_users, credentials, withdraw_requests, config, user_balances, user_info, user_language, created_accounts, cancel_tracking

    channel_id = config.get("channel_id")
    if not channel_id:
        logger.warning("Backup channel ID not set")
        return

    try:
        s = requests.Session()
        resp = s.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getChat?chat_id={channel_id}",
            timeout=20
        ).json()
        if not resp.get("ok"):
            logger.error("Cannot access channel")
            return

        pinned = resp["result"].get("pinned_message")
        if not pinned:
            logger.info("No pinned backup")
            return

        compressed = None
        if "document" in pinned:
            file_id = pinned["document"]["file_id"]
            file_info = s.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}",
                timeout=20
            ).json()
            if not file_info.get("ok"):
                return
            file_path = file_info["result"]["file_path"]
            compressed = s.get(
                f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}",
                timeout=60
            ).content
            last_backup_part_ids = []
        elif "text" in pinned:
            index = json.loads(pinned["text"])
            part_ids = index.get("parts", [])
            if not part_ids:
                return
            combined = bytearray()
            for part_msg_id in part_ids:
                msg_resp = s.get(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/getMessage?chat_id={channel_id}&message_id={part_msg_id}",
                    timeout=20
                ).json()
                if not msg_resp.get("ok") or "document" not in msg_resp.get("result", {}):
                    logger.error(f"Part {part_msg_id} not found")
                    return
                file_id = msg_resp["result"]["document"]["file_id"]
                file_info = s.get(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}",
                    timeout=20
                ).json()
                if not file_info.get("ok"):
                    return
                file_path = file_info["result"]["file_path"]
                part_content = s.get(
                    f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}",
                    timeout=60
                ).content
                combined.extend(part_content)
            compressed = bytes(combined)
            last_backup_part_ids = part_ids
        else:
            return

        if not compressed:
            return

        decompressed = gzip.decompress(compressed)
        data = json.loads(decompressed.decode('utf-8'))

        with data_lock:
            subscribed_users = set(data.get("subscribed_users", []))
            credentials = data.get("credentials", [])
            withdraw_requests = data.get("withdraw_requests", [])
            created_accounts = data.get("created_accounts", [])
            if "config" in data:
                for k in data["config"]:
                    config[k] = data["config"][k]
            user_balances = data.get("user_balances", {})
            user_info = data.get("user_info", {})
            user_language = data.get("user_language", {})
            cancel_tracking = data.get("cancel_tracking", {})
            last_backup_message_id = pinned["message_id"]
            save_all()

        logger.info(f"Data restored: {len(subscribed_users)} users, {len(credentials)} credentials, {len(created_accounts)} created accounts")
    except Exception as e:
        logger.error(f"Auto restore error: {e}")

def auto_backup_loop():
    while True:
        time.sleep(86400)
        save_data_to_channel()

# ================== ADMIN FUNCTIONS ==================
def admin_panel(chat_id):
    send_message(t("admin_panel", chat_id), chat_id, reply_markup=admin_keyboard())

def admin_add_creds_prompt(chat_id):
    admin_cred_upload_session[chat_id] = {"step": "email"}
    send_message(
        t("add_accounts", chat_id),
        chat_id,
        reply_markup={"keyboard": [["❌ Cancel" if get_lang(chat_id) == "en" else "❌ বাতিল"]], "resize_keyboard": True}
    )

def process_admin_creds_email(chat_id, text):
    if chat_id not in admin_cred_upload_session or admin_cred_upload_session[chat_id].get("step") != "email":
        return False
    emails = [line.strip() for line in text.strip().splitlines() if line.strip()]
    valid_emails = [e for e in emails if '@' in e]
    if not valid_emails:
        send_message(t("no_valid_emails", chat_id), chat_id)
        return True
    admin_cred_upload_session[chat_id]["emails"] = valid_emails
    admin_cred_upload_session[chat_id]["step"] = "password"
    send_message(t("enter_password", chat_id), chat_id)
    return True

def process_admin_creds_password(chat_id, text):
    if chat_id not in admin_cred_upload_session or admin_cred_upload_session[chat_id].get("step") != "password":
        return False
    password = text.strip()
    if not password:
        send_message(t("password_empty", chat_id), chat_id)
        return True
    emails = admin_cred_upload_session[chat_id]["emails"]
    added = 0
    with data_lock:
        for email in emails:
            credentials.append({"email": email, "password": password, "used": False, "assigned_to": None})
            added += 1
        save_all()
    del admin_cred_upload_session[chat_id]
    send_message(t("add_accounts_success", chat_id, added=added, total=len(credentials)), chat_id, reply_markup=admin_keyboard())
    return True

def admin_list_creds(chat_id, page=0, message_id=None):
    total = len(credentials)
    if total == 0:
        lang = get_lang(chat_id)
        msg = "📭 **No credentials available.**" if lang == "en" else "📭 **কোনো ক্রেডেনশিয়াল নেই।**"
        send_message(msg, chat_id, reply_markup=admin_keyboard())
        return
    per_page = 10
    total_pages = (total + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = min(start + per_page, total)
    page_items = credentials[start:end]
    lang = get_lang(chat_id)
    lines = [t("account_list", chat_id, page=page+1, total_pages=total_pages)]
    status_text = {"en": {"used": "🔴 Used ({user})", "unused": "🟢 Unused"},
                   "bn": {"used": "🔴 ব্যবহৃত ({user})", "unused": "🟢 অব্যবহৃত"}}
    st = status_text.get(lang, status_text["en"])
    for idx, cred in enumerate(page_items, start=start):
        if cred.get("used", False):
            status = st["used"].format(user=cred.get("assigned_to", "N/A"))
        else:
            status = st["unused"]
        lines.append(f"`{cred['email']}` | `{cred['password']}` | {status}")
    text = "\n".join(lines)
    kb = {"inline_keyboard": []}
    nav = []
    if page > 0:
        nav.append({"text": "⬅️ Previous" if lang == "en" else "⬅️ আগের", "callback_data": f"credp_{page-1}"})
    if page < total_pages - 1:
        nav.append({"text": "Next ➡️" if lang == "en" else "পরের ➡️", "callback_data": f"credp_{page+1}"})
    if nav:
        kb["inline_keyboard"].append(nav)
    for i, cred in enumerate(page_items, start=start):
        kb["inline_keyboard"].append([{"text": f"🗑️ {i+1}. {cred['email']}", "callback_data": f"delc_{i}"}])
    kb["inline_keyboard"].append([{"text": "🔙 Close" if lang == "en" else "🔙 বন্ধ", "callback_data": "close_list"}])
    if message_id:
        edit_message_text(chat_id, message_id, text, reply_markup=kb)
    else:
        send_message(text, chat_id, reply_markup=kb)

def admin_delete_single_cred(chat_id, index, message_id):
    deleted = delete_credential_by_index(index)
    if deleted:
        lang = get_lang(chat_id)
        msg = f"🗑️ `{deleted['email']}` deleted." if lang == "en" else f"🗑️ `{deleted['email']}` ডিলিট করা হয়েছে।"
        send_message(msg, chat_id)
        admin_list_creds(chat_id, page=0, message_id=message_id)
    else:
        send_message("❌ Credential not found.", chat_id)

def admin_delete_all_creds(chat_id):
    with data_lock:
        count = len(credentials)
        credentials.clear()
        save_all()
    send_message(t("deleted_all", chat_id, count=count), chat_id, reply_markup=admin_keyboard())

def admin_stats(chat_id):
    total_creds = len(credentials)
    used_creds = len([c for c in credentials if c.get("used", False)])
    pending_withdraw = len([w for w in withdraw_requests if w["status"] == "pending"])
    pending_acc = len([a for a in created_accounts if a.get("status") == "pending"])
    send_message(
        t("stats", chat_id,
          users=len(subscribed_users),
          total=total_creds,
          used=used_creds,
          pending=pending_withdraw,
          price=config.get("base_balance", 10.0),
          pending_acc=pending_acc),
        chat_id, reply_markup=admin_keyboard()
    )

def admin_show_withdraw_requests(chat_id, page=0, message_id=None):
    pending = [w for w in withdraw_requests if w["status"] == "pending"]
    if not pending:
        send_message(t("no_pending", chat_id), chat_id, reply_markup=admin_keyboard())
        return
    per_page = 5
    total = len(pending)
    total_pages = (total + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = min(start + per_page, total)
    page_items = pending[start:end]
    lang = get_lang(chat_id)
    msg = t("pending_withdraws", chat_id)
    for w in page_items:
        time_str = datetime.fromtimestamp(w['timestamp']).strftime("%d/%m %H:%M")
        msg += t("pending_item", chat_id,
                 id=w['id'],
                 user=w['user_id'],
                 amount=w['amount'],
                 method=w['method'].upper(),
                 account=w['account'],
                 time=time_str) + "\n"
    kb = {"inline_keyboard": []}
    for w in page_items:
        kb["inline_keyboard"].append([
            {"text": f"✅ Approve {w['id'][:6]}", "callback_data": f"appw_{w['id']}"},
            {"text": f"❌ Reject {w['id'][:6]}", "callback_data": f"rejw_{w['id']}"}
        ])
    nav = []
    if page > 0:
        nav.append({"text": "⬅️ Previous" if lang == "en" else "⬅️ আগের", "callback_data": f"wpage_{page-1}"})
    if page < total_pages - 1:
        nav.append({"text": "Next ➡️" if lang == "en" else "পরের ➡️", "callback_data": f"wpage_{page+1}"})
    if nav:
        kb["inline_keyboard"].append(nav)
    kb["inline_keyboard"].append([{"text": "🔙 Close" if lang == "en" else "🔙 বন্ধ", "callback_data": "close_withdraw"}])
    if message_id:
        edit_message_text(chat_id, message_id, msg, reply_markup=kb)
    else:
        send_message(msg, chat_id, reply_markup=kb)

def admin_approve_withdraw(chat_id, w_id):
    with data_lock:
        for w in withdraw_requests:
            if w["id"] == w_id and w["status"] == "pending":
                w["status"] = "approved"
                save_all()
                send_message(t("approve_success", chat_id, w_id=w_id), chat_id, reply_markup=admin_keyboard())
                send_message(
                    f"✅ Your withdraw of **{w['amount']}** BDT has been approved.",
                    w["user_id"]
                )
                return
    send_message(t("not_found", chat_id, w_id=w_id), chat_id)

def admin_reject_withdraw(chat_id, w_id):
    with data_lock:
        for w in withdraw_requests:
            if w["id"] == w_id and w["status"] == "pending":
                w["status"] = "rejected"
                add_balance(w["user_id"], w["amount"])
                save_all()
                send_message(t("reject_success", chat_id, w_id=w_id), chat_id, reply_markup=admin_keyboard())
                send_message(
                    f"❌ Your withdraw request was rejected. **{w['amount']}** BDT has been refunded.",
                    w["user_id"]
                )
                return
    send_message(t("not_found", chat_id, w_id=w_id), chat_id)

def admin_set_price(chat_id, text):
    parts = text.split()
    if len(parts) != 2:
        send_message("❌ **Usage:** `/setprice <amount>`", chat_id)
        return
    try:
        price = float(parts[1])
        if price <= 0:
            raise ValueError
        config["base_balance"] = price
        save_all()
        send_message(t("price_set", chat_id, price=price), chat_id, reply_markup=admin_keyboard())
    except:
        send_message("❌ Invalid amount.", chat_id)

def admin_set_rules(chat_id, text):
    parts = text.split(" ", 1)
    if len(parts) != 2:
        send_message("❌ **Usage:** `/setrules <new rules>`", chat_id)
        return
    lang = get_lang(chat_id)
    if lang == "bn":
        config["work_rules_bn"] = parts[1]
    else:
        config["work_rules_en"] = parts[1]
    save_all()
    send_message(t("rules_updated", chat_id), chat_id, reply_markup=admin_keyboard())

def admin_set_channel(chat_id, text):
    parts = text.split()
    if len(parts) != 2:
        send_message("❌ **Usage:** `/setchannel <channel_id>`", chat_id)
        return
    config["channel_id"] = parts[1]
    save_all()
    send_message(t("channel_set", chat_id, channel=parts[1]), chat_id, reply_markup=admin_keyboard())

def admin_toggle_maintenance(chat_id):
    current = config.get("maintenance_mode", False)
    config["maintenance_mode"] = not current
    save_all()
    status = "enabled" if config["maintenance_mode"] else "disabled"
    lang = get_lang(chat_id)
    msg = f"🔧 **Maintenance mode {status}.**" if lang == "en" else f"🔧 **রক্ষণাবেক্ষণ মোড { 'চালু' if config['maintenance_mode'] else 'বন্ধ'}।**"
    send_message(msg, chat_id, reply_markup=admin_keyboard())

def admin_backup(chat_id):
    send_message(t("backup_creating", chat_id), chat_id)
    success = manual_backup(chat_id)
    if not success:
        send_message("❌ Backup failed. Please try again.", chat_id)

def admin_restore(chat_id):
    lang = get_lang(chat_id)
    msg = "📥 **Restore from file**\n\nPlease upload the `.json.gz` backup file." if lang == "en" else "📥 **ফাইল থেকে রিস্টোর**\n\nদয়া করে `.json.gz` ব্যাকআপ ফাইল আপলোড করুন।"
    send_message(msg, chat_id, reply_markup={"keyboard": [["❌ Cancel" if lang == "en" else "❌ বাতিল"]], "resize_keyboard": True})
    set_session(chat_id, {"restore_mode": True})

def process_restore_file(chat_id, file_content):
    success = manual_restore(chat_id, file_content)
    if success:
        send_message(t("restore_completed", chat_id), chat_id, reply_markup=admin_keyboard())
    else:
        send_message("❌ Restore failed. Invalid file format.", chat_id, reply_markup=admin_keyboard())
    clear_session(chat_id)

# ================== PENDING APPROVALS (VIEW-ONLY) ==================
def admin_show_pending_approvals(chat_id, page=0, message_id=None):
    pending = [acc for acc in created_accounts if acc.get("status") == "pending"]
    if not pending:
        send_message(t("no_pending_approvals", chat_id), chat_id, reply_markup=admin_keyboard())
        return

    per_page = 10
    total = len(pending)
    total_pages = (total + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = min(start + per_page, total)
    page_items = pending[start:end]

    lang = get_lang(chat_id)
    msg = t("pending_approvals_list", chat_id, page=page+1, total_pages=total_pages)
    for acc in page_items:
        time_str = datetime.fromtimestamp(acc["timestamp"]).strftime("%d/%m %H:%M")
        msg += t("pending_approval_item", chat_id,
                 id=acc.get("id", "N/A"),
                 user=acc.get("user_id", "N/A"),
                 username=acc.get("username", "N/A"),
                 email=acc.get("email", "N/A"),
                 time=time_str)

    kb = {"inline_keyboard": []}
    nav = []
    if page > 0:
        nav.append({"text": "⬅️ Previous" if lang == "en" else "⬅️ আগের", "callback_data": f"papage_{page-1}"})
    if page < total_pages - 1:
        nav.append({"text": "Next ➡️" if lang == "en" else "পরের ➡️", "callback_data": f"papage_{page+1}"})
    if nav:
        kb["inline_keyboard"].append(nav)
    kb["inline_keyboard"].append([{"text": "🔙 Close" if lang == "en" else "🔙 বন্ধ", "callback_data": "close_papprovals"}])

    if message_id:
        edit_message_text(chat_id, message_id, msg, reply_markup=kb)
    else:
        send_message(msg, chat_id, reply_markup=kb)

def admin_export_excel(chat_id):
    if not created_accounts:
        send_message("📭 No accounts to export.", chat_id, reply_markup=admin_keyboard())
        return
    send_message("⏳ Generating Excel file...", chat_id)
    try:
        excel_data = generate_created_accounts_excel()
        filename = f"created_accounts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        resp = send_document(excel_data, filename, chat_id, caption=t("excel_exported", chat_id))
        if not resp or resp.status_code != 200:
            send_message("❌ Failed to generate Excel.", chat_id)
    except Exception as e:
        logger.error(f"Excel export error: {e}")
        send_message("❌ Error generating Excel.", chat_id)

# ================== UPLOAD LISTS HANDLERS ==================
def admin_upload_approved_prompt(chat_id):
    set_session(chat_id, {"upload_mode": "approved"})
    send_message(t("upload_approved_prompt", chat_id), chat_id, reply_markup=cancel_keyboard(chat_id))

def admin_upload_rejected_prompt(chat_id):
    set_session(chat_id, {"upload_mode": "rejected"})
    send_message(t("upload_rejected_prompt", chat_id), chat_id, reply_markup=cancel_keyboard(chat_id))

def process_uploaded_list(chat_id, text_or_file_content):
    session = get_session(chat_id)
    if not session or session.get("upload_mode") not in ["approved", "rejected"]:
        return False
    mode = session["upload_mode"]
    usernames = parse_username_list(text_or_file_content)
    if not usernames:
        send_message(t("upload_no_usernames", chat_id), chat_id)
        return True

    if mode == "approved":
        approved, not_found, already = process_approve_list(usernames)
        summary = t("upload_approved_summary", chat_id, approved=approved, not_found=not_found, already=already)
    else:
        rejected, not_found, already = process_reject_list(usernames)
        summary = t("upload_rejected_summary", chat_id, rejected=rejected, not_found=not_found, already=already)

    send_message(summary, chat_id, reply_markup=admin_keyboard())
    clear_session(chat_id)
    return True

# ================== CLEAR EXPORTED ACCOUNTS ==================
def admin_clear_created_accounts(chat_id):
    with data_lock:
        count = len(created_accounts)
        created_accounts.clear()
        save_json(CREATED_ACCOUNTS_FILE, created_accounts)
    send_message(t("clear_exported_success", chat_id, count=count), chat_id, reply_markup=admin_keyboard())

# ================== USER MY ACCOUNTS ==================
def show_my_accounts(chat_id, page=0, message_id=None):
    uid = str(chat_id)
    my_accs = [a for a in created_accounts if a.get("user_id") == uid]
    if not my_accs:
        send_message(t("no_my_accounts", chat_id), chat_id)
        return
    per_page = 5
    total_pages = (len(my_accs) + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = min(start + per_page, len(my_accs))
    page_items = my_accs[start:end]
    lines = [t("my_accounts_list", chat_id, page=page+1, total_pages=total_pages)]
    for acc in page_items:
        time_str = datetime.fromtimestamp(acc["timestamp"]).strftime("%d/%m %H:%M")
        status = acc.get("status", "pending")
        twofa = acc.get("twofa", "N/A")
        lines.append(t("my_account_item", chat_id,
                       username=acc.get("username","N/A"),
                       email=acc.get("email","N/A"),
                       password=acc.get("password","N/A"),
                       twofa=twofa,
                       status=status,
                       time=time_str))
    text = "\n".join(lines)
    kb = {"inline_keyboard": []}
    nav = []
    if page > 0:
        nav.append({"text": "⬅️", "callback_data": f"myacc_{page-1}"})
    if page < total_pages - 1:
        nav.append({"text": "➡️", "callback_data": f"myacc_{page+1}"})
    if nav:
        kb["inline_keyboard"].append(nav)
    kb["inline_keyboard"].append([{"text": "🔙 Close", "callback_data": "close_myacc"}])
    if message_id:
        edit_message_text(chat_id, message_id, text, reply_markup=kb)
    else:
        send_message(text, chat_id, reply_markup=kb)

# ================== ADMIN USER LIST & BAN MANAGEMENT ==================
def admin_show_user_list(chat_id, page=0, message_id=None):
    users = list(subscribed_users)
    if not users:
        send_message(t("no_users", chat_id), chat_id)
        return
    per_page = 10
    total_pages = (len(users) + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = min(start + per_page, len(users))
    page_users = users[start:end]
    lines = [t("user_list", chat_id, page=page+1, total_pages=total_pages)]
    for uid in page_users:
        balance = user_balances.get(str(uid), 0.0)
        total_acc = user_info.get(str(uid), {}).get("total_accounts", 0)
        banned, _ = is_banned(uid)
        ban_status = "🔴 Banned" if banned else "🟢 Active"
        lines.append(t("user_list_item", chat_id,
                       id=uid,
                       balance=balance,
                       total=total_acc,
                       status=ban_status))
    text = "\n".join(lines)
    kb = {"inline_keyboard": []}
    nav = []
    if page > 0:
        nav.append({"text": "⬅️", "callback_data": f"usrlist_{page-1}"})
    if page < total_pages - 1:
        nav.append({"text": "➡️", "callback_data": f"usrlist_{page+1}"})
    if nav:
        kb["inline_keyboard"].append(nav)
    for uid in page_users:
        banned, _ = is_banned(uid)
        if not banned:
            kb["inline_keyboard"].append([{"text": f"🔨 Ban {uid}", "callback_data": f"banuser_{uid}"}])
        else:
            kb["inline_keyboard"].append([{"text": f"✅ Unban {uid}", "callback_data": f"unbanuser_{uid}"}])
    kb["inline_keyboard"].append([{"text": "🔙 Close", "callback_data": "close_userlist"}])
    if message_id:
        edit_message_text(chat_id, message_id, text, reply_markup=kb)
    else:
        send_message(text, chat_id, reply_markup=kb)

def admin_show_banned_users(chat_id, page=0, message_id=None):
    banned_users = [uid for uid in subscribed_users if is_banned(uid)[0]]
    if not banned_users:
        send_message(t("no_banned_users", chat_id), chat_id)
        return
    per_page = 10
    total_pages = (len(banned_users) + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = min(start + per_page, len(banned_users))
    page_users = banned_users[start:end]
    lines = [t("banned_list", chat_id, page=page+1, total_pages=total_pages)]
    for uid in page_users:
        banned, ban_end = is_banned(uid)
        if ban_end == -1:
            ban_time = "Manual Ban"
        else:
            remaining = get_ban_remaining(uid)
            if remaining > 0:
                hours = remaining // 3600
                minutes = (remaining % 3600) // 60
                ban_time = f"{hours}h {minutes}m"
            else:
                ban_time = "Unknown"
        lines.append(t("banned_user_item", chat_id, id=uid, ban_time=ban_time))
    text = "\n".join(lines)
    kb = {"inline_keyboard": []}
    nav = []
    if page > 0:
        nav.append({"text": "⬅️", "callback_data": f"banlist_{page-1}"})
    if page < total_pages - 1:
        nav.append({"text": "➡️", "callback_data": f"banlist_{page+1}"})
    if nav:
        kb["inline_keyboard"].append(nav)
    for uid in page_users:
        kb["inline_keyboard"].append([{"text": f"✅ Unban {uid}", "callback_data": f"unbanuser_{uid}"}])
    kb["inline_keyboard"].append([{"text": "🔙 Close", "callback_data": "close_banlist"}])
    if message_id:
        edit_message_text(chat_id, message_id, text, reply_markup=kb)
    else:
        send_message(text, chat_id, reply_markup=kb)

# ================== BROADCAST ==================
def admin_broadcast_prompt(chat_id):
    set_session(chat_id, {"broadcast_mode": True})
    send_message(t("broadcast_prompt", chat_id), chat_id, reply_markup=cancel_keyboard(chat_id))

# ================== USER COMMANDS ==================
def start_command(chat_id, chat_type="private"):
    if chat_id not in subscribed_users:
        with data_lock:
            subscribed_users.add(chat_id)
            save_all()
    uid = str(chat_id)
    if uid not in user_info:
        user_info[uid] = {"name": f"User_{chat_id}", "total_accounts": 0}
        save_json(USER_INFO_FILE, user_info)
    if uid not in user_language:
        user_language[uid] = "en"
        save_json(LANGUAGE_FILE, user_language)
    if chat_type == "private":
        send_message(t("welcome", chat_id), chat_id, reply_markup=main_keyboard(chat_id))
    else:
        lang = get_lang(chat_id)
        msg = "🤖 I work in private chats. Please `/start` me in private." if lang == "en" else "🤖 আমি প্রাইভেট চ্যাটে কাজ করি। দয়া করে প্রাইভেটে `/start` দিন।"
        send_message(msg, chat_id)

def change_language(chat_id):
    current = get_lang(chat_id)
    new_lang = "bn" if current == "en" else "en"
    set_lang(chat_id, new_lang)
    send_message(t("language_changed", chat_id), chat_id, reply_markup=main_keyboard(chat_id))

def instagram_work(chat_id):
    if config.get("maintenance_mode", False) and str(chat_id) != ADMIN_CHAT_ID:
        send_message(t("under_maintenance", chat_id), chat_id)
        return
    banned, _ = is_banned(chat_id)
    if banned:
        remaining = get_ban_remaining(chat_id)
        if remaining == -1:
            send_message("⛔ You are permanently banned by admin.", chat_id)
        else:
            hours = remaining // 3600
            minutes = (remaining % 3600) // 60
            send_message(t("banned_work", chat_id, hours=hours, minutes=minutes), chat_id)
        return
    session = get_session(chat_id)
    if session and session.get("active"):
        send_message(t("active_session", chat_id), chat_id)
        return
    rules = config.get("work_rules_en" if get_lang(chat_id) == "en" else "work_rules_bn", "")
    send_message(t("work", chat_id, rules=rules), chat_id, reply_markup=work_start_keyboard(chat_id))

def start_work(chat_id):
    if config.get("maintenance_mode", False) and str(chat_id) != ADMIN_CHAT_ID:
        send_message(t("under_maintenance", chat_id), chat_id)
        return
    banned, _ = is_banned(chat_id)
    if banned:
        remaining = get_ban_remaining(chat_id)
        if remaining == -1:
            send_message("⛔ You are permanently banned by admin.", chat_id)
        else:
            hours = remaining // 3600
            minutes = (remaining % 3600) // 60
            send_message(t("banned_work", chat_id, hours=hours, minutes=minutes), chat_id)
        return
    cred = get_available_credential()
    if not cred:
        send_message(t("no_accounts", chat_id), chat_id, reply_markup=main_keyboard(chat_id))
        return
    assign_credential_to_user(cred, chat_id)

    set_session(chat_id, {
        "active": True,
        "step": "username_input",
        "email": cred["email"],
        "password": cred["password"],
        "username": None,
        "twofa": None
    })
    send_message(t("account_assigned", chat_id, email=cred["email"], password=cred["password"]),
                 chat_id, reply_markup=username_input_keyboard(chat_id))

def process_username(chat_id, text):
    session = get_session(chat_id)
    if not session or not session.get("active") or session.get("step") != "username_input":
        return False
    if text in ["👤 Enter Username", "👤 ইউজারনেম দিন"]:
        send_message(t("enter_username_prompt", chat_id), chat_id)
        return True
    username = text.strip()
    if not username:
        send_message("❌ Username cannot be empty. Please enter again:", chat_id)
        return True
    session["username"] = username
    session["step"] = "twofa"
    set_session(chat_id, session)
    send_message(t("twofa_prompt", chat_id), chat_id, reply_markup=cancel_keyboard(chat_id))
    return True

def process_twofa(chat_id, text):
    session = get_session(chat_id)
    if not session or not session.get("active") or session.get("step") != "twofa":
        return False

    original_key = text.strip()
    clean_key = ''.join(original_key.split())

    if not clean_key:
        send_message("❌ **সিক্রেট কী খালি রাখা যাবে না।** দয়া করে সঠিক কী দিন।", chat_id)
        return True

    try:
        totp = pyotp.TOTP(clean_key)
        current_code = totp.now()
        logger.info(f"2FA code generated for user {chat_id}: {current_code}")
        session["twofa"] = original_key
        session["twofa_code"] = current_code
    except Exception as e:
        send_message(
            "❌ **ভুল 2FA সিক্রেট কী!** দয়া করে সঠিক Base32 ফরম্যাটের কী দিন।\n"
            "উদাহরণ: `JBSWY3DPEHPK3PXP` (স্পেস থাকলেও চলবে, বট নিজেই ঠিক করবে)",
            chat_id
        )
        return True

    session["step"] = "follow"
    set_session(chat_id, session)

    send_message(
        t("twofa_verified", chat_id, code=current_code),
        chat_id, reply_markup=yes_no_keyboard(chat_id)
    )
    return True

def process_follow_yes(chat_id):
    session = get_session(chat_id)
    if not session or not session.get("active") or session.get("step") != "follow":
        return
    session["step"] = "done"
    set_session(chat_id, session)
    username = session.get("username", "N/A")
    twofa_secret = session.get("twofa", "N/A")
    save_created_account(username, session["email"], session["password"], twofa_secret, chat_id)
    reset_consecutive_cancels(chat_id)
    send_message(
        t("completed", chat_id,
          username=username,
          email=session['email'],
          password=session['password'],
          twofa=twofa_secret),
        chat_id, reply_markup=next_or_cancel_keyboard(chat_id)
    )
    clear_session(chat_id)

def process_follow_no(chat_id):
    send_message(t("follow_yes", chat_id), chat_id, reply_markup=yes_no_keyboard(chat_id))

def show_balance(chat_id):
    send_message(get_balance_text(chat_id), chat_id)

def withdraw_start(chat_id):
    if config.get("maintenance_mode", False) and str(chat_id) != ADMIN_CHAT_ID:
        send_message(t("under_maintenance", chat_id), chat_id)
        return
    clear_session(chat_id)
    send_message(t("withdraw", chat_id), chat_id, reply_markup=withdraw_method_keyboard(chat_id))

def withdraw_request(chat_id, method):
    if config.get("maintenance_mode", False) and str(chat_id) != ADMIN_CHAT_ID:
        send_message(t("under_maintenance", chat_id), chat_id)
        return
    set_session(chat_id, {"withdraw_step": "account", "method": method})
    send_message(t("enter_account", chat_id, method=method.upper()), chat_id, reply_markup=cancel_keyboard(chat_id))

def process_withdraw_account(chat_id, text):
    session = get_session(chat_id)
    if not session or session.get("withdraw_step") != "account":
        return False
    session["account"] = text.strip()
    session["withdraw_step"] = "amount"
    set_session(chat_id, session)
    send_message(t("enter_amount", chat_id), chat_id, reply_markup=cancel_keyboard(chat_id))
    return True

def process_withdraw_amount(chat_id, text):
    session = get_session(chat_id)
    if not session or session.get("withdraw_step") != "amount":
        return False
    try:
        amount = float(text.strip())
        if amount <= 0:
            raise ValueError
    except:
        send_message(t("invalid_amount", chat_id), chat_id)
        return False
    uid = str(chat_id)
    if user_balances.get(uid, 0.0) < amount:
        send_message(t("insufficient", chat_id), chat_id)
        return False
    w_id = uuid.uuid4().hex[:10]
    withdraw_requests.append({
        "id": w_id, "user_id": chat_id, "amount": amount,
        "method": session["method"], "account": session["account"],
        "status": "pending", "timestamp": time.time()
    })
    deduct_balance(chat_id, amount)
    save_all()
    send_message(t("withdraw_submitted", chat_id, w_id=w_id, amount=amount), chat_id, reply_markup=main_keyboard(chat_id))
    lang = get_lang(chat_id)
    admin_msg = f"📥 **New Withdraw Request**\n🆔 `{w_id}`\n👤 User: `{chat_id}`\n💰 {amount} BDT\n💳 {session['method'].upper()}\n📞 {session['account']}"
    if lang == "bn":
        admin_msg = f"📥 **নতুন উইথড্র রিকোয়েস্ট**\n🆔 `{w_id}`\n👤 ইউজার: `{chat_id}`\n💰 {amount} টাকা\n💳 {session['method'].upper()}\n📞 {session['account']}"
    send_message(admin_msg, ADMIN_CHAT_ID)
    clear_session(chat_id)
    return True

# ================== UPDATE HANDLER ==================
last_update_id = 0

def handle_updates():
    global last_update_id
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
            if last_update_id:
                params["offset"] = last_update_id + 1
            resp = requests.get(url, params=params, timeout=(5, 30)).json()
            if resp.get("ok") and resp.get("result"):
                for update in resp["result"]:
                    last_update_id = update["update_id"]
                    process_update(update)
        except requests.exceptions.Timeout:
            logger.warning("getUpdates timeout, retrying...")
        except Exception as e:
            logger.error(f"Update loop error: {e}")
        time.sleep(0.02)

def process_update(update):
    if "message" in update:
        msg = update["message"]
        chat_id = str(msg["chat"]["id"])
        chat_type = msg["chat"]["type"]
        text = msg.get("text", "").strip()

        if text == "/start":
            start_command(chat_id, chat_type)
            return

        if chat_type != "private":
            return

        if chat_id not in subscribed_users:
            with data_lock:
                subscribed_users.add(chat_id)
                save_all()

        # ===== ADMIN SECTION =====
        if chat_id == ADMIN_CHAT_ID:
            session = get_session(chat_id)
            # Handle restore mode
            if session and session.get("restore_mode"):
                if text == "❌ Cancel" or text == "❌ বাতিল":
                    clear_session(chat_id)
                    send_message("❌ Cancelled." if get_lang(chat_id) == "en" else "❌ বাতিল করা হয়েছে।", chat_id, reply_markup=admin_keyboard())
                    return
                if "document" in msg:
                    file_obj = msg["document"]
                    if file_obj.get("file_name", "").endswith(".json.gz"):
                        try:
                            file_id = file_obj["file_id"]
                            file_info = requests.get(
                                f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}"
                            ).json()
                            file_path = file_info["result"]["file_path"]
                            file_content = requests.get(
                                f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
                            ).content
                            process_restore_file(chat_id, file_content)
                        except Exception as e:
                            logger.error(f"Restore file error: {e}")
                            send_message("❌ Failed to read file.", chat_id)
                    else:
                        send_message("❌ Please upload a `.json.gz` file.", chat_id)
                    return

            # Handle upload list mode (approved/rejected)
            if session and session.get("upload_mode") in ["approved", "rejected"]:
                if text == "/cancel" or text == "❌ Cancel" or text == "❌ বাতিল":
                    clear_session(chat_id)
                    send_message("❌ Cancelled." if get_lang(chat_id) == "en" else "❌ বাতিল করা হয়েছে।", chat_id, reply_markup=admin_keyboard())
                    return
                if "document" in msg:
                    file_obj = msg["document"]
                    if file_obj.get("mime_type") == "text/plain" or file_obj.get("file_name", "").endswith(".txt"):
                        try:
                            file_id = file_obj["file_id"]
                            file_info = requests.get(
                                f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}"
                            ).json()
                            file_path = file_info["result"]["file_path"]
                            file_content = requests.get(
                                f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
                            ).text
                            process_uploaded_list(chat_id, file_content)
                        except Exception as e:
                            logger.error(f"File read error: {e}")
                            send_message("❌ Failed to read file. Please send a valid text file.", chat_id)
                    else:
                        send_message("❌ Please upload a .txt file or send the list as text.", chat_id)
                    return
                else:
                    process_uploaded_list(chat_id, text)
                    return

            # Handle broadcast mode
            if session and session.get("broadcast_mode"):
                if text == "/cancel" or text == "❌ Cancel" or text == "❌ বাতিল":
                    clear_session(chat_id)
                    send_message(t("broadcast_cancelled", chat_id), chat_id, reply_markup=admin_keyboard())
                    return
                # Send broadcast to all users
                for uid in list(subscribed_users):
                    try:
                        send_message(text, uid)
                    except:
                        pass
                send_message(t("broadcast_success", chat_id), chat_id, reply_markup=admin_keyboard())
                clear_session(chat_id)
                return

            # Admin normal commands
            if chat_id in admin_cred_upload_session:
                if text == "/cancel" or text == "❌ Cancel" or text == "❌ বাতিল":
                    del admin_cred_upload_session[chat_id]
                    send_message("❌ Cancelled." if get_lang(chat_id) == "en" else "❌ বাতিল করা হয়েছে।", chat_id, reply_markup=admin_keyboard())
                    return
                step = admin_cred_upload_session[chat_id].get("step")
                if step == "email":
                    process_admin_creds_email(chat_id, text)
                elif step == "password":
                    process_admin_creds_password(chat_id, text)
                return

            if text == "⚙️ Admin Panel":
                admin_panel(chat_id)
                return
            if text == "➕ Add Accounts":
                admin_add_creds_prompt(chat_id)
                return
            if text == "📋 Account List":
                admin_list_creds(chat_id)
                return
            if text == "🗑️ Delete All":
                admin_delete_all_creds(chat_id)
                return
            if text == "📊 Statistics":
                admin_stats(chat_id)
                return
            if text == "📥 Withdraw Requests":
                admin_show_withdraw_requests(chat_id)
                return
            if text == "📁 Backup":
                admin_backup(chat_id)
                return
            if text == "📥 Restore":
                admin_restore(chat_id)
                return
            if text == "💲 Set Price":
                send_message("/setprice <amount>", chat_id, reply_markup=admin_keyboard())
                return
            if text == "📝 Edit Rules":
                send_message("/setrules <new rules>", chat_id, reply_markup=admin_keyboard())
                return
            if text.startswith("🔧 Maintenance"):
                admin_toggle_maintenance(chat_id)
                return
            if text == "📋 Pending Approvals":
                admin_show_pending_approvals(chat_id)
                return
            if text == "📥 Export Excel":
                admin_export_excel(chat_id)
                return
            if text == "📤 Upload Approved":
                admin_upload_approved_prompt(chat_id)
                return
            if text == "📤 Upload Rejected":
                admin_upload_rejected_prompt(chat_id)
                return
            if text == "🗑️ Clear Exported Accounts":
                admin_clear_created_accounts(chat_id)
                return
            if text == "🔙 Main Menu":
                send_message("Main Menu", chat_id, reply_markup=main_keyboard(chat_id))
                return
            if text == "👥 User List":
                admin_show_user_list(chat_id)
                return
            if text == "🚫 Banned Users":
                admin_show_banned_users(chat_id)
                return
            if text == "📢 Broadcast":
                admin_broadcast_prompt(chat_id)
                return

            if text.startswith("/setprice"):
                admin_set_price(chat_id, text)
                return
            if text.startswith("/setrules"):
                admin_set_rules(chat_id, text)
                return
            if text.startswith("/setchannel"):
                admin_set_channel(chat_id, text)
                return
            if text.startswith("/approve_withdraw"):
                parts = text.split()
                if len(parts) == 2:
                    admin_approve_withdraw(chat_id, parts[1])
                return
            if text.startswith("/reject_withdraw"):
                parts = text.split()
                if len(parts) == 2:
                    admin_reject_withdraw(chat_id, parts[1])
                return
            if text.startswith("/unban"):
                parts = text.split()
                if len(parts) == 2:
                    try:
                        target_id = int(parts[1])
                        unban_user(target_id)
                        send_message(t("unbanned_user", chat_id, user_id=target_id), chat_id, reply_markup=admin_keyboard())
                    except:
                        send_message("❌ Invalid user ID.", chat_id)
                else:
                    send_message("Usage: /unban <user_id>", chat_id)
                return

        # ===== USER SECTION =====
        session = get_session(chat_id)

        if text == "🌐 Language":
            change_language(chat_id)
            return

        if text in ["❌ Cancel", "❌ বাতিল"]:
            if session and session.get("active"):
                record_cancel(chat_id)
            clear_session(chat_id)
            send_message(t("cancelled", chat_id), chat_id, reply_markup=main_keyboard(chat_id))
            return

        if text == "📱 Instagram Work":
            instagram_work(chat_id)
            return
        if text == "💰 Balance":
            show_balance(chat_id)
            return
        if text == "💸 Withdraw":
            withdraw_start(chat_id)
            return
        if text == "🚀 Start" or text == "🚀 শুরু করুন":
            start_work(chat_id)
            return
        if text == "✅ Yes" or text == "✅ হ্যাঁ":
            process_follow_yes(chat_id)
            return
        if text == "❌ No" or text == "❌ না":
            process_follow_no(chat_id)
            return
        if text == "🔄 Open Another Account" or text == "🔄 আরেকটি অ্যাকাউন্ট খুলুন":
            clear_session(chat_id)
            instagram_work(chat_id)
            return
        if text == "💸 bKash":
            withdraw_request(chat_id, "bkash")
            return
        if text == "💸 Nagad":
            withdraw_request(chat_id, "nagad")
            return
        if text == "📜 My Accounts":
            show_my_accounts(chat_id)
            return
        if text == "📞 Support":
            send_message(t("support_message", chat_id), chat_id)
            return

        if session:
            if session.get("withdraw_step") == "account":
                process_withdraw_account(chat_id, text)
                return
            if session.get("withdraw_step") == "amount":
                process_withdraw_amount(chat_id, text)
                return
            if session.get("active"):
                step = session.get("step")
                if step == "username_input":
                    if text in ["👤 Enter Username", "👤 ইউজারনেম দিন"]:
                        send_message(t("enter_username_prompt", chat_id), chat_id)
                        return
                    process_username(chat_id, text)
                    return
                if step == "twofa":
                    process_twofa(chat_id, text)
                    return

        send_message(t("unknown", chat_id), chat_id, reply_markup=main_keyboard(chat_id))

    elif "callback_query" in update:
        cb = update["callback_query"]
        chat_id = str(cb["message"]["chat"]["id"])
        data = cb["data"]
        message_id = cb["message"]["message_id"]
        answer_callback(cb["id"])

        if data.startswith("credp_"):
            page = int(data.split("_")[1])
            admin_list_creds(chat_id, page=page, message_id=message_id)
        elif data.startswith("delc_"):
            index = int(data.split("_")[1])
            admin_delete_single_cred(chat_id, index, message_id)
        elif data == "close_list":
            delete_message(chat_id, message_id)
            lang = get_lang(chat_id)
            msg = "📋 List closed." if lang == "en" else "📋 তালিকা বন্ধ করা হয়েছে।"
            send_message(msg, chat_id, reply_markup=admin_keyboard())
        elif data.startswith("appw_"):
            w_id = data[5:]
            admin_approve_withdraw(chat_id, w_id)
            admin_show_withdraw_requests(chat_id, page=0, message_id=message_id)
        elif data.startswith("rejw_"):
            w_id = data[5:]
            admin_reject_withdraw(chat_id, w_id)
            admin_show_withdraw_requests(chat_id, page=0, message_id=message_id)
        elif data.startswith("wpage_"):
            page = int(data.split("_")[1])
            admin_show_withdraw_requests(chat_id, page=page, message_id=message_id)
        elif data == "close_withdraw":
            delete_message(chat_id, message_id)
            lang = get_lang(chat_id)
            msg = "📋 Withdraw list closed." if lang == "en" else "📋 উইথড্র তালিকা বন্ধ করা হয়েছে।"
            send_message(msg, chat_id, reply_markup=admin_keyboard())
        elif data.startswith("papage_"):
            page = int(data.split("_")[1])
            admin_show_pending_approvals(chat_id, page=page, message_id=message_id)
        elif data == "close_papprovals":
            delete_message(chat_id, message_id)
            lang = get_lang(chat_id)
            msg = "📋 Pending approvals list closed." if lang == "en" else "📋 পেন্ডিং অ্যাপ্রুভাল তালিকা বন্ধ করা হয়েছে।"
            send_message(msg, chat_id, reply_markup=admin_keyboard())
        elif data.startswith("myacc_"):
            page = int(data.split("_")[1])
            show_my_accounts(chat_id, page=page, message_id=message_id)
        elif data == "close_myacc":
            delete_message(chat_id, message_id)
            send_message("Closed.", chat_id, reply_markup=main_keyboard(chat_id))
        elif data.startswith("usrlist_"):
            page = int(data.split("_")[1])
            admin_show_user_list(chat_id, page=page, message_id=message_id)
        elif data.startswith("banlist_"):
            page = int(data.split("_")[1])
            admin_show_banned_users(chat_id, page=page, message_id=message_id)
        elif data == "close_userlist":
            delete_message(chat_id, message_id)
            send_message("Closed.", chat_id, reply_markup=admin_keyboard())
        elif data == "close_banlist":
            delete_message(chat_id, message_id)
            send_message("Closed.", chat_id, reply_markup=admin_keyboard())
        elif data.startswith("banuser_"):
            uid = data.split("_",1)[1]
            manual_ban_user(uid)
            send_message(t("ban_success", chat_id, user_id=uid), chat_id)
            try:
                send_message("You have been banned by admin.", uid)
            except:
                pass
            delete_message(chat_id, message_id)
            admin_show_user_list(chat_id)
        elif data.startswith("unbanuser_"):
            uid = data.split("_",1)[1]
            unban_user(uid)
            send_message(t("unban_success", chat_id, user_id=uid), chat_id)
            try:
                send_message("You have been unbanned by admin.", uid)
            except:
                pass
            delete_message(chat_id, message_id)
            admin_show_banned_users(chat_id)

# ================== FLASK ==================
@app.route("/")
def home():
    return "🤖 Bot is Running!"

# ================== MAIN ==================
if __name__ == "__main__":
    load_all()
    logger.info(f"Loaded: {len(subscribed_users)} users, {len(credentials)} credentials, {len(created_accounts)} created accounts")
    auto_restore_from_channel()
    threading.Thread(target=auto_backup_loop, daemon=True).start()
    threading.Thread(target=handle_updates, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)

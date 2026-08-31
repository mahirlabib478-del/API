import os
import time
import json
import logging
import threading
import uuid
import gzip
import requests
from flask import Flask
from datetime import datetime
import openpyxl
from openpyxl.styles import Font, Alignment
from io import BytesIO

# ================== CONFIGURATION ==================
BOT_TOKEN = "8692984075:AAEpPTKdqD5dgGGBfYYpLPPyi26U93qVnzI"
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

# ================== GLOBALS ==================
subscribed_users = set()
user_sessions = {}
credentials = []
withdraw_requests = []
created_accounts = []
config = {
    "base_balance": 10.0,
    "channel_id": CHANNEL_ID,
    "maintenance_mode": False,
    "work_rules_en": "📱 **Instagram Account Opening Guidelines**\n\n"
                     "1️⃣ You will receive an email and password.\n"
                     "2️⃣ Enter your Instagram username.\n"
                     "3️⃣ Enter 2FA code if required.\n"
                     "4️⃣ Follow 5 profiles and set profile picture.\n"
                     "5️⃣ Upon completion, your balance will be credited.",
    "work_rules_bn": "📱 **Instagram অ্যাকাউন্ট খোলার নিয়মাবলী**\n\n"
                     "1️⃣ আপনি একটি ইমেইল ও পাসওয়ার্ড পাবেন।\n"
                     "2️⃣ আপনার Instagram ইউজারনেম দিন।\n"
                     "3️⃣ 2FA কোড দিন (যদি থাকে)।\n"
                     "4️⃣ ৫টি প্রোফাইল ফলো করুন ও প্রোফাইল পিক সেট করুন।\n"
                     "5️⃣ সম্পন্ন হলে আপনার ব্যালেন্স যোগ হবে।"
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
    global subscribed_users, user_sessions, credentials, withdraw_requests, config, user_balances, user_info, user_language, created_accounts
    subscribed_users = set(load_json(USERS_FILE, []))
    user_sessions = load_json(SESSIONS_FILE, {})
    credentials = load_json(CREDENTIALS_FILE, [])
    withdraw_requests = load_json(WITHDRAWS_FILE, [])
    created_accounts = load_json(CREATED_ACCOUNTS_FILE, [])
    user_balances = load_json(USER_BALANCES_FILE, {})
    user_info = load_json(USER_INFO_FILE, {})
    user_language = load_json(LANGUAGE_FILE, {})
    
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
        return requests.post(url, json=payload, timeout=10)
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
            "twofa_prompt": "🔐 **Please enter your 2FA code:**",
            "twofa_verified": "✅ **2FA verified!**\n\nNow follow **5 profiles & set profile picture**. Have you done that?",
            "follow_yes": "⚠️ **Please follow 5 profiles & set profile picture** then press **Yes**.",
            "completed": "🎉 **Congratulations! Account opening completed!**\n\n👤 **Username:** `{username}`\n📧 **Email:** `{email}`\n🔑 **Password:** `{password}`\n🔐 **2FA:** {twofa}\n\n💰 **{price}** BDT added to your balance.",
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
            "stats": "📊 **Statistics**\n\n👥 Users: {users}\n📦 Credentials: {total} (Used: {used})\n💸 Pending Withdraw: {pending}\n💰 Price per account: {price} BDT",
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
            "created_accounts_list": "📁 **Created Accounts** (Page {page}/{total_pages})\n\n",
            "created_account_item": "👤 {username} | 📧 {email} | 🔑 {password} | 🔐 {twofa} | 👤 {user_id} | 🕒 {time}",
            "created_accounts_empty": "📭 **No created accounts yet.**",
            "export_excel": "📥 **Export Excel**\n\nClick below to download all created accounts as Excel file.",
            "excel_exported": "✅ **Excel file exported!**",
        },
        "bn": {
            "welcome": "🤖 **Instagram অ্যাকাউন্ট খোলার বট**\n\nআমি আপনাকে ইনস্টাগ্রাম অ্যাকাউন্ট খুলতে সাহায্য করি।\nনিচের বাটন ব্যবহার করে শুরু করুন।",
            "balance": "💰 **আপনার ব্যালেন্স**\n\nব্যালেন্স: `{balance:.2f}` টাকা\nমোট অ্যাকাউন্ট খোলা: {total}",
            "withdraw": "💸 **উইথড্র করুন**\n\nআপনার টাকা উত্তোলনের মাধ্যম নির্বাচন করুন:",
            "enter_account": "📞 আপনার {method} অ্যাকাউন্ট নম্বর দিন:",
            "enter_amount": "💰 **কত টাকা উইথড্র করতে চান?**",
            "withdraw_submitted": "✅ **উইথড্র রিকোয়েস্ট জমা হয়েছে!**\n🆔 **আইডি:** `{w_id}`\n💰 **পরিমাণ:** {amount} টাকা\n📌 স্ট্যাটাস: ⏳ পেন্ডিং",
            "insufficient": "❌ **অপর্যাপ্ত ব্যালেন্স!**",
            "invalid_amount": "❌ **সঠিক টাকার পরিমাণ দিন।**",
            "account_assigned": "✅ **আপনার জন্য একটি অ্যাকাউন্ট বরাদ্দ করা হয়েছে:**\n\n📧 **ইমেইল:** `{email}`\n🔑 **পাসওয়ার্ড:** `{password}`\n\nনিচের বাটনে ক্লিক করে আপনার **Instagram ইউজারনেম** দিন:",
            "enter_username_prompt": "👤 **আপনার প্রকৃত Instagram ইউজারনেম দিন:**",
            "twofa_prompt": "🔐 **2FA কোড দিন:**",
            "twofa_verified": "✅ **2FA প্রক্রিয়া সম্পন্ন!**\n\nএখন **৫টি প্রোফাইল ফলো ও প্রোফাইল পিক সেট** করুন। সম্পন্ন করেছেন?",
            "follow_yes": "⚠️ **দয়া করে ৫টি প্রোফাইল ফলো ও প্রোফাইল পিক সেট করুন** এবং তারপর **হ্যাঁ** বাটন চাপুন।",
            "completed": "🎉 **অভিনন্দন! অ্যাকাউন্ট খোলা সম্পন্ন হয়েছে!**\n\n👤 **ইউজারনেম:** `{username}`\n📧 **ইমেইল:** `{email}`\n🔑 **পাসওয়ার্ড:** `{password}`\n🔐 **2FA:** {twofa}\n\n💰 **{price}** টাকা আপনার ব্যালেন্সে যোগ হয়েছে।",
            "no_accounts": "❌ **কোনো অব্যবহৃত অ্যাকাউন্ট নেই!** অ্যাডমিনের সাথে যোগাযোগ করুন।",
            "active_session": "⏳ **আপনার একটি চলমান সেশন আছে।** আগে সেটি শেষ করুন বা বাতিল করুন।",
            "under_maintenance": "🔧 **বট রক্ষণাবেক্ষণে রয়েছে। পরে চেষ্টা করুন।**",
            "cancelled": "❌ বাতিল করা হয়েছে।",
            "unknown": "❌ আমি বুঝতে পারিনি। দয়া করে নিচের বাটন ব্যবহার করুন।",
            "work": "📱 **Instagram Work**\n\n{rules}\n\n👇 **Start** বাটনে ক্লিক করুন।",
            "language_changed": "🌐 ভাষা পরিবর্তন করে বাংলা করা হয়েছে।",
            "admin_panel": "⚙️ **অ্যাডমিন কন্ট্রোল প্যানেল**",
            "add_accounts": "📧 **ইমেইল লিস্ট দিন**\n\nপ্রতি লাইনে একটি:\n`user1@gmail.com`\n`user2@yahoo.com`\n\nবাতিল করতে /cancel লিখুন।",
            "add_accounts_success": "✅ **{added} টি অ্যাকাউন্ট যোগ করা হয়েছে।** মোট: {total}",
            "no_valid_emails": "❌ **কোনো বৈধ ইমেইল পাওয়া যায়নি।**",
            "enter_password": "🔑 **পাসওয়ার্ড দিন** (সব অ্যাকাউন্টের জন্য একই):",
            "password_empty": "❌ **পাসওয়ার্ড খালি রাখা যাবে না।**",
            "account_list": "📋 **অ্যাকাউন্ট লিস্ট** (পৃষ্ঠা {page}/{total_pages})\n\n",
            "account_item": "`{email}` | `{password}` | {status}",
            "deleted_all": "🗑️ **{count} টি অ্যাকাউন্ট ডিলিট করা হয়েছে।**",
            "stats": "📊 **পরিসংখ্যান**\n\n👥 ইউজার: {users}\n📦 ক্রেডেনশিয়াল: {total} (ব্যবহৃত: {used})\n💸 পেন্ডিং উইথড্র: {pending}\n💰 প্রতি অ্যাকাউন্ট মূল্য: {price} টাকা",
            "no_pending": "📭 **কোনো পেন্ডিং উইথড্র রিকোয়েস্ট নেই।**",
            "pending_withdraws": "📥 **পেন্ডিং উইথড্র রিকোয়েস্ট**\n\n",
            "pending_item": "🆔 `{id}`\n👤 ইউজার: `{user}`\n💰 পরিমাণ: `{amount}` টাকা\n💳 মাধ্যম: {method}\n📞 অ্যাকাউন্ট: `{account}`\n🕒 সময়: {time}\n",
            "approve_success": "✅ **উইথড্র {w_id} অনুমোদিত হয়েছে।**",
            "reject_success": "❌ **উইথড্র {w_id} বাতিল করা হয়েছে।**",
            "not_found": "❌ {w_id} পাওয়া যায়নি।",
            "price_set": "✅ প্রতি অ্যাকাউন্ট খোলার ইনকাম **{price}** টাকা সেট করা হয়েছে।",
            "rules_updated": "✅ **নিয়মাবলী আপডেট করা হয়েছে।**",
            "channel_set": "✅ চ্যানেল আইডি `{channel}` সেট করা হয়েছে।",
            "backup_created": "✅ **ব্যাকআপ তৈরি হয়েছে!**",
            "restore_completed": "✅ **রিস্টোর সম্পন্ন!**",
            "backup_creating": "⏳ **ব্যাকআপ নেওয়া হচ্ছে...**",
            "restoring": "⏳ **ডেটা রিস্টোর করা হচ্ছে...**",
            "created_accounts_list": "📁 **তৈরি করা অ্যাকাউন্ট** (পৃষ্ঠা {page}/{total_pages})\n\n",
            "created_account_item": "👤 {username} | 📧 {email} | 🔑 {password} | 🔐 {twofa} | 👤 {user_id} | 🕒 {time}",
            "created_accounts_empty": "📭 **এখনো কোনো অ্যাকাউন্ট তৈরি হয়নি।**",
            "export_excel": "📥 **এক্সেল এক্সপোর্ট**\n\nনিচের বাটনে ক্লিক করে সব অ্যাকাউন্টের Excel ফাইল ডাউনলোড করুন।",
            "excel_exported": "✅ **Excel ফাইল তৈরি হয়েছে!**",
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

# ================== KEYBOARDS ==================
def main_keyboard(chat_id):
    lang = get_lang(chat_id)
    kb = [
        ["📱 Instagram Work", "💰 Balance"],
        ["💸 Withdraw", "🌐 Language"]
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
            ["📁 Created Accounts", "📥 Export Excel"],
            ["🔙 Main Menu"]
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

# ================== NEW: USERNAME INPUT KEYBOARD ==================
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
        created_accounts.append({
            "username": username,
            "email": email,
            "password": password,
            "twofa": twofa,
            "user_id": str(user_id),
            "timestamp": time.time()
        })
        save_json(CREATED_ACCOUNTS_FILE, created_accounts)
    trigger_backup()

def generate_created_accounts_excel():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Created Accounts"
    headers = ["Username", "Email", "Password", "2FA", "User ID", "Timestamp"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")
    for acc in created_accounts:
        time_str = datetime.fromtimestamp(acc["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
        ws.append([acc["username"], acc["email"], acc["password"], acc["twofa"], acc["user_id"], time_str])
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
            save_all()
        logger.info(f"Manual restore completed: {len(subscribed_users)} users, {len(credentials)} credentials, {len(created_accounts)} created accounts")
        return True
    except Exception as e:
        logger.error(f"Manual restore error: {e}")
        return False

def auto_restore_from_channel():
    global last_backup_message_id, last_backup_part_ids
    global subscribed_users, credentials, withdraw_requests, config, user_balances, user_info, user_language, created_accounts

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
            last_backup_message_id = pinned["message_id"]
            save_all()

        logger.info(f"Data restored: {len(subscribed_users)} users, {len(credentials)} credentials, {len(created_accounts)} created accounts")
    except Exception as e:
        logger.error(f"Auto restore error: {e}")

def auto_backup_loop():
    while True:
        time.sleep(300)
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
        msg = "📭 **No credentials available.**" if lang == "en" else "📭 **কোনো ক্রেডেনশিয়াল নেই।**"
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
        msg = f"🗑️ `{deleted['email']}` deleted." if lang == "en" else f"🗑️ `{deleted['email']}` ডিলিট করা হয়েছে।"
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
    total_created = len(created_accounts)
    send_message(
        t("stats", chat_id,
          users=len(subscribed_users),
          total=total_creds,
          used=used_creds,
          pending=pending_withdraw,
          price=config.get("base_balance", 10.0)) + f"\n📁 Created Accounts: {total_created}",
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
    msg = "📥 **Restore from file**\n\nPlease upload the `.json.gz` backup file." if lang == "en" else "📥 **ফাইল থেকে রিস্টোর**\n\nদয়া করে `.json.gz` ব্যাকআপ ফাইল আপলোড করুন।"
    send_message(msg, chat_id, reply_markup={"keyboard": [["❌ Cancel" if lang == "en" else "❌ বাতিল"]], "resize_keyboard": True})
    set_session(chat_id, {"restore_mode": True})

def process_restore_file(chat_id, file_content):
    success = manual_restore(chat_id, file_content)
    if success:
        send_message(t("restore_completed", chat_id), chat_id, reply_markup=admin_keyboard())
    else:
        send_message("❌ Restore failed. Invalid file format.", chat_id, reply_markup=admin_keyboard())
    clear_session(chat_id)

def admin_list_created_accounts(chat_id, page=0, message_id=None):
    try:
        total = len(created_accounts)
        logger.info(f"admin_list_created_accounts called, total: {total}")
        if total == 0:
            msg = t("created_accounts_empty", chat_id)
            send_message(msg, chat_id, reply_markup=admin_keyboard())
            return

        per_page = 10
        total_pages = (total + per_page - 1) // per_page
        page = max(0, min(page, total_pages - 1))
        start = page * per_page
        end = min(start + per_page, total)
        page_items = created_accounts[start:end]
        lang = get_lang(chat_id)
        lines = [t("created_accounts_list", chat_id, page=page+1, total_pages=total_pages)]
        for acc in page_items:
            time_str = datetime.fromtimestamp(acc["timestamp"]).strftime("%d/%m %H:%M")
            lines.append(t("created_account_item", chat_id,
                           username=acc["username"],
                           email=acc["email"],
                           password=acc["password"],
                           twofa=acc["twofa"],
                           user_id=acc["user_id"],
                           time=time_str))
        text = "\n".join(lines)
        kb = {"inline_keyboard": []}
        nav = []
        if page > 0:
            nav.append({"text": "⬅️ Previous" if lang == "en" else "⬅️ আগের", "callback_data": f"capage_{page-1}"})
        if page < total_pages - 1:
            nav.append({"text": "Next ➡️" if lang == "en" else "পরের ➡️", "callback_data": f"capage_{page+1}"})
        if nav:
            kb["inline_keyboard"].append(nav)
        kb["inline_keyboard"].append([{"text": "🔙 Close" if lang == "en" else "🔙 বন্ধ", "callback_data": "close_calist"}])
        if message_id:
            edit_message_text(chat_id, message_id, text, reply_markup=kb)
        else:
            send_message(text, chat_id, reply_markup=kb)
    except Exception as e:
        logger.error(f"Error in admin_list_created_accounts: {e}")
        send_message("❌ An error occurred while fetching created accounts.", chat_id, reply_markup=admin_keyboard())

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
        msg = "🤖 I work in private chats. Please `/start` me in private." if lang == "en" else "🤖 আমি প্রাইভেট চ্যাটে কাজ করি। দয়া করে প্রাইভেটে `/start` দিন।"
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
                 chat_id, reply_markup=username_input_keyboard(chat_id))  # New keyboard

def process_username(chat_id, text):
    session = get_session(chat_id)
    if not session or not session.get("active") or session.get("step") != "username_input":
        return False
    # Ensure the user didn't just click the button again
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
    session["twofa"] = text.strip()
    session["step"] = "follow"
    set_session(chat_id, session)
    send_message(t("twofa_verified", chat_id), chat_id, reply_markup=yes_no_keyboard(chat_id))
    return True

def process_follow_yes(chat_id):
    session = get_session(chat_id)
    if not session or not session.get("active") or session.get("step") != "follow":
        return
    session["step"] = "done"
    set_session(chat_id, session)
    price = config.get("base_balance", 10.0)
    add_balance(chat_id, price)
    uid = str(chat_id)
    if uid in user_info:
        user_info[uid]["total_accounts"] = user_info[uid].get("total_accounts", 0) + 1
        save_json(USER_INFO_FILE, user_info)
    username = session.get("username", "N/A")
    twofa = session.get("twofa", "N/A")
    save_created_account(username, session["email"], session["password"], twofa, chat_id)
    send_message(
        t("completed", chat_id,
          username=username,
          email=session['email'],
          password=session['password'],
          twofa=twofa,
          price=price),
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
            resp = requests.get(url, params=params, timeout=35).json()
            if resp.get("ok") and resp.get("result"):
                for update in resp["result"]:
                    last_update_id = update["update_id"]
                    process_update(update)
        except Exception as e:
            logger.error(f"Update loop error: {e}")
        time.sleep(1)

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
            if session and session.get("restore_mode"):
                if text == "❌ Cancel" or text == "❌ বাতিল":
                    clear_session(chat_id)
                    send_message("❌ Cancelled." if get_lang(chat_id) == "en" else "❌ বাতিল করা হয়েছে।", chat_id, reply_markup=admin_keyboard())
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

            if chat_id in admin_cred_upload_session:
                if text == "/cancel" or text == "❌ Cancel" or text == "❌ বাতিল":
                    del admin_cred_upload_session[chat_id]
                    send_message("❌ Cancelled." if get_lang(chat_id) == "en" else "❌ বাতিল করা হয়েছে।", chat_id, reply_markup=admin_keyboard())
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
            if text == "📁 Created Accounts":
                admin_list_created_accounts(chat_id)
                return
            if text == "📥 Export Excel":
                admin_export_excel(chat_id)
                return
            if text == "🔙 Main Menu":
                send_message("Main Menu", chat_id, reply_markup=main_keyboard(chat_id))
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

        # ===== USER SECTION =====
        session = get_session(chat_id)

        if text == "🌐 Language":
            change_language(chat_id)
            return
        if text == "❌ Cancel" or text == "❌ বাতিল":
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

        # Session processing
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
                    # If user clicks the username button, prompt them
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
            msg = "📋 List closed." if lang == "en" else "📋 তালিকা বন্ধ করা হয়েছে।"
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
            msg = "📋 Withdraw list closed." if lang == "en" else "📋 উইথড্র তালিকা বন্ধ করা হয়েছে।"
            send_message(msg, chat_id, reply_markup=admin_keyboard())
        elif data.startswith("capage_"):
            page = int(data.split("_")[1])
            admin_list_created_accounts(chat_id, page=page, message_id=message_id)
        elif data == "close_calist":
            delete_message(chat_id, message_id)
            lang = get_lang(chat_id)
            msg = "📁 Created accounts list closed." if lang == "en" else "📁 তৈরি অ্যাকাউন্টের তালিকা বন্ধ করা হয়েছে।"
            send_message(msg, chat_id, reply_markup=admin_keyboard())

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

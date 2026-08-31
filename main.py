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

# ================== CONFIGURATION ==================
BOT_TOKEN = "8692984075:AAEpPTKdqD5dgGGBfYYpLPPyi26U93qVnzI"
ADMIN_CHAT_ID = "8538304896"

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

# ================== GLOBALS ==================
subscribed_users = set()
user_sessions = {}
credentials = []
withdraw_requests = []
config = {
    "base_balance": 10.0,
    "channel_id": "",
    "work_rules": "📱 **Instagram Account Opening Guidelines**\n\n"
                  "1️⃣ You will receive an email and password.\n"
                  "2️⃣ Enter 2FA code if required.\n"
                  "3️⃣ Follow 5 profiles.\n"
                  "4️⃣ Upon completion, your balance will be credited."
}
user_balances = {}
user_info = {}

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
    global subscribed_users, user_sessions, credentials, withdraw_requests, config, user_balances, user_info
    subscribed_users = set(load_json(USERS_FILE, []))
    user_sessions = load_json(SESSIONS_FILE, {})
    credentials = load_json(CREDENTIALS_FILE, [])
    withdraw_requests = load_json(WITHDRAWS_FILE, [])
    user_balances = load_json(USER_BALANCES_FILE, {})
    user_info = load_json(USER_INFO_FILE, {})
    
    cfg = load_json(CONFIG_FILE, {})
    for k in config:
        if k not in cfg:
            cfg[k] = config[k]
    config = cfg

def save_all():
    save_json(USERS_FILE, list(subscribed_users))
    save_json(SESSIONS_FILE, user_sessions)
    save_json(CREDENTIALS_FILE, credentials)
    save_json(WITHDRAWS_FILE, withdraw_requests)
    save_json(CONFIG_FILE, config)
    save_json(USER_BALANCES_FILE, user_balances)
    save_json(USER_INFO_FILE, user_info)
    trigger_backup()

# ================== DEBOUNCED BACKUP ==================
def trigger_backup():
    global save_pending, save_timer
    with data_lock:
        if not save_pending:
            save_pending = True
            if save_timer:
                save_timer.cancel()
            save_timer = threading.Timer(5.0, execute_backup)
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
        files = {'document': (filename, file_bytes, 'application/gzip')}
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
    return (
        f"💰 **Your Balance**\n\n"
        f"Balance: `{balance:.2f}` BDT\n"
        f"Total Accounts Opened: {total_accounts}"
    )

# ================== KEYBOARDS ==================
def main_keyboard(chat_id):
    kb = [
        ["📱 Instagram Work", "💰 Balance"],
        ["💸 Withdraw"]
    ]
    if str(chat_id) == ADMIN_CHAT_ID:
        kb.append(["⚙️ Admin Panel"])
    return {"keyboard": kb, "resize_keyboard": True}

def admin_keyboard():
    return {
        "keyboard": [
            ["➕ Add Accounts", "📋 Account List"],
            ["🗑️ Delete All", "📊 Statistics"],
            ["💲 Set Price", "📝 Edit Rules"],
            ["📥 Withdraw Requests", "📁 Backup"],
            ["📥 Restore", "🔙 Main Menu"]
        ],
        "resize_keyboard": True
    }

def twofa_reply_keyboard():
    """2FA এর জন্য কীপ্যাড কিবোর্ড"""
    return {
        "keyboard": [
            ["🔐 Enter 2FA"],
            ["❌ Cancel"]
        ],
        "resize_keyboard": True
    }

def yes_no_keyboard():
    return {
        "keyboard": [
            ["✅ Yes", "❌ No"]
        ],
        "resize_keyboard": True
    }

def next_or_cancel_keyboard():
    return {
        "keyboard": [
            ["🔄 Open Another Account"],
            ["❌ Cancel"]
        ],
        "resize_keyboard": True
    }

def withdraw_method_keyboard():
    return {
        "keyboard": [
            ["💸 bKash", "💸 Nagad"],
            ["❌ Cancel"]
        ],
        "resize_keyboard": True
    }

def cancel_keyboard():
    return {
        "keyboard": [
            ["❌ Cancel"]
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

def save_data_to_channel():
    global last_backup_message_id, last_backup_part_ids
    channel_id = config.get("channel_id")
    if not channel_id:
        logger.warning("Backup channel ID not set!")
        return

    with backup_lock:
        try:
            with data_lock:
                data = {
                    "subscribed_users": list(subscribed_users),
                    "credentials": credentials,
                    "withdraw_requests": withdraw_requests,
                    "config": config,
                    "user_balances": user_balances,
                    "user_info": user_info,
                    "timestamp": datetime.now().isoformat()
                }
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

def auto_backup_loop():
    while True:
        time.sleep(300)
        save_data_to_channel()

def auto_restore_from_channel():
    global last_backup_message_id, last_backup_part_ids
    global subscribed_users, credentials, withdraw_requests, config, user_balances, user_info

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
            if "config" in data:
                for k in data["config"]:
                    config[k] = data["config"][k]
            user_balances = data.get("user_balances", {})
            user_info = data.get("user_info", {})
            last_backup_message_id = pinned["message_id"]
            save_all()

        logger.info(f"Data restored: {len(subscribed_users)} users, {len(credentials)} credentials")
    except Exception as e:
        logger.error(f"Auto restore error: {e}")

# ================== ADMIN FUNCTIONS ==================
def admin_panel(chat_id):
    send_message("⚙️ **Admin Control Panel**", chat_id, reply_markup=admin_keyboard())

def admin_add_creds_prompt(chat_id):
    admin_cred_upload_session[chat_id] = {"step": "email"}
    send_message(
        "📧 **Enter Email List**\n\nOne per line:\n`user1@gmail.com`\n`user2@yahoo.com`\n\nType /cancel to abort.",
        chat_id,
        reply_markup={"keyboard": [["❌ Cancel"]], "resize_keyboard": True}
    )

def process_admin_creds_email(chat_id, text):
    if chat_id not in admin_cred_upload_session or admin_cred_upload_session[chat_id].get("step") != "email":
        return False
    
    emails = [line.strip() for line in text.strip().splitlines() if line.strip()]
    valid_emails = [e for e in emails if '@' in e]
    
    if not valid_emails:
        send_message("❌ **No valid emails found.**", chat_id)
        return True
    
    admin_cred_upload_session[chat_id]["emails"] = valid_emails
    admin_cred_upload_session[chat_id]["step"] = "password"
    
    msg = f"✅ **{len(valid_emails)} emails accepted.**"
    msg += "\n\n🔑 **Enter the password** (same for all accounts):"
    send_message(msg, chat_id)
    return True

def process_admin_creds_password(chat_id, text):
    if chat_id not in admin_cred_upload_session or admin_cred_upload_session[chat_id].get("step") != "password":
        return False
    
    password = text.strip()
    if not password:
        send_message("❌ **Password cannot be empty.**", chat_id)
        return True
    
    emails = admin_cred_upload_session[chat_id]["emails"]
    added = 0
    with data_lock:
        for email in emails:
            credentials.append({
                "email": email,
                "password": password,
                "used": False,
                "assigned_to": None
            })
            added += 1
        save_all()
    
    del admin_cred_upload_session[chat_id]
    send_message(
        f"✅ **{added} accounts added.** Total: {len(credentials)}",
        chat_id, reply_markup=admin_keyboard()
    )
    return True

def admin_list_creds(chat_id):
    total = len(credentials)
    used = len([c for c in credentials if c.get("used", False)])
    available = total - used
    send_message(
        f"📋 **Account List**\n\n"
        f"Total: {total}\n"
        f"Used: {used}\n"
        f"Available: {available}",
        chat_id, reply_markup=admin_keyboard()
    )

def admin_delete_all_creds(chat_id):
    with data_lock:
        count = len(credentials)
        credentials.clear()
        save_all()
    send_message(f"🗑️ **{count} accounts deleted.**", chat_id, reply_markup=admin_keyboard())

def admin_stats(chat_id):
    total_creds = len(credentials)
    used_creds = len([c for c in credentials if c.get("used", False)])
    pending_withdraw = len([w for w in withdraw_requests if w["status"] == "pending"])
    send_message(
        f"📊 **Statistics**\n\n"
        f"👥 Users: {len(subscribed_users)}\n"
        f"📦 Credentials: {total_creds} (Used: {used_creds})\n"
        f"💸 Pending Withdraw: {pending_withdraw}\n"
        f"💰 Price per account: {config.get('base_balance', 10.0)} BDT",
        chat_id, reply_markup=admin_keyboard()
    )

def admin_show_withdraw_requests(chat_id):
    pending = [w for w in withdraw_requests if w["status"] == "pending"]
    if not pending:
        send_message("📭 **No pending withdraw requests.**", chat_id, reply_markup=admin_keyboard())
        return
    msg = "📥 **Pending Withdraw Requests**\n\n"
    for w in pending:
        msg += f"🆔 `{w['id']}` | 👤 `{w['user_id']}` | 💰 `{w['amount']}` | 💳 {w['method'].upper()}\n"
    send_message(msg, chat_id)

def admin_approve_withdraw(chat_id, w_id):
    with data_lock:
        for w in withdraw_requests:
            if w["id"] == w_id and w["status"] == "pending":
                w["status"] = "approved"
                save_all()
                send_message(f"✅ **Withdraw {w_id} approved.**", chat_id, reply_markup=admin_keyboard())
                send_message(
                    f"✅ Your withdraw of **{w['amount']}** BDT has been approved.",
                    w["user_id"]
                )
                return
    send_message(f"❌ {w_id} not found.", chat_id)

def admin_reject_withdraw(chat_id, w_id):
    with data_lock:
        for w in withdraw_requests:
            if w["id"] == w_id and w["status"] == "pending":
                w["status"] = "rejected"
                add_balance(w["user_id"], w["amount"])
                save_all()
                send_message(f"❌ **Withdraw {w_id} rejected.**", chat_id, reply_markup=admin_keyboard())
                send_message(
                    f"❌ Your withdraw request was rejected. **{w['amount']}** BDT has been refunded.",
                    w["user_id"]
                )
                return
    send_message(f"❌ {w_id} not found.", chat_id)

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
        send_message(f"✅ Price per account set to **{price}** BDT.", chat_id, reply_markup=admin_keyboard())
    except:
        send_message("❌ Invalid amount.", chat_id)

def admin_set_rules(chat_id, text):
    parts = text.split(" ", 1)
    if len(parts) != 2:
        send_message("❌ **Usage:** `/setrules <new rules>`", chat_id)
        return
    config["work_rules"] = parts[1]
    save_all()
    send_message("✅ **Rules updated.**", chat_id, reply_markup=admin_keyboard())

def admin_set_channel(chat_id, text):
    parts = text.split()
    if len(parts) != 2:
        send_message("❌ **Usage:** `/setchannel <channel_id>`", chat_id)
        return
    config["channel_id"] = parts[1]
    save_all()
    send_message(f"✅ Channel ID set to `{parts[1]}`.", chat_id, reply_markup=admin_keyboard())

def admin_backup(chat_id):
    send_message("⏳ **Creating backup...**", chat_id)
    save_data_to_channel()
    send_message("✅ **Backup completed!**", chat_id, reply_markup=admin_keyboard())

def admin_restore(chat_id):
    send_message("⏳ **Restoring data...**", chat_id)
    auto_restore_from_channel()
    send_message("✅ **Restore completed!**", chat_id, reply_markup=admin_keyboard())

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
    
    if chat_type == "private":
        send_message(
            "🤖 **Instagram Account Opener Bot**\n\n"
            "I help you open Instagram accounts.\n"
            "Use the buttons below to start.",
            chat_id, reply_markup=main_keyboard(chat_id)
        )
    else:
        send_message("🤖 I work in private chats. Please `/start` me in private.", chat_id)

def instagram_work(chat_id):
    session = get_session(chat_id)
    if session and session.get("active"):
        send_message("⏳ You already have an active session. Please finish or cancel it.", chat_id)
        return
    rules = config.get("work_rules", "📱 Instagram Account Opening Guidelines")
    send_message(
        f"{rules}\n\n"
        f"👇 Press **Start** to begin.",
        chat_id, reply_markup={"keyboard": [["🚀 Start"], ["❌ Cancel"]], "resize_keyboard": True}
    )

def start_work(chat_id):
    cred = get_available_credential()
    if not cred:
        send_message("❌ **No available accounts!** Please contact admin.", chat_id, reply_markup=main_keyboard(chat_id))
        return
    assign_credential_to_user(cred, chat_id)
    set_session(chat_id, {
        "active": True,
        "step": "credentials_shown",
        "email": cred["email"],
        "password": cred["password"],
        "twofa": None
    })
    
    send_message(
        f"✅ **Your account has been assigned:**\n\n"
        f"📧 **Email:** `{cred['email']}`\n"
        f"🔑 **Password:** `{cred['password']}`\n\n"
        f"🔐 Press the button below to enter your 2FA code:",
        chat_id, reply_markup=twofa_reply_keyboard()
    )

def prompt_twofa(chat_id):
    session = get_session(chat_id)
    if not session or not session.get("active"):
        return
    session["step"] = "twofa_input"
    set_session(chat_id, session)
    
    send_message(
        "🔐 **Please enter your 2FA code:**",
        chat_id, reply_markup=cancel_keyboard()
    )

def process_twofa(chat_id, text):
    session = get_session(chat_id)
    if not session or not session.get("active") or session.get("step") != "twofa_input":
        return False
    
    session["twofa"] = text.strip()
    session["step"] = "follow"
    set_session(chat_id, session)
    
    send_message(
        "✅ **2FA verified!**\n\n"
        "Now follow **5 profiles**. Have you followed them?",
        chat_id, reply_markup=yes_no_keyboard()
    )
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
    send_message(
        f"🎉 **Congratulations! Account opening completed!**\n\n"
        f"📧 **Email:** `{session['email']}`\n"
        f"🔑 **Password:** `{session['password']}`\n"
        f"🔐 **2FA:** {session.get('twofa', 'N/A')}\n\n"
        f"💰 **{price}** BDT added to your balance.",
        chat_id, reply_markup=next_or_cancel_keyboard()
    )
    clear_session(chat_id)

def process_follow_no(chat_id):
    send_message(
        "⚠️ **Please follow 5 profiles** and then press **Yes**.",
        chat_id, reply_markup=yes_no_keyboard()
    )

def show_balance(chat_id):
    text = get_balance_text(chat_id)
    send_message(text, chat_id)

def withdraw_start(chat_id):
    clear_session(chat_id)
    send_message(
        "💸 **Withdraw**\n\nChoose your withdrawal method:",
        chat_id, reply_markup=withdraw_method_keyboard()
    )

def withdraw_request(chat_id, method):
    set_session(chat_id, {"withdraw_step": "account", "method": method})
    send_message(
        f"📞 Enter your {method.upper()} account number:",
        chat_id, reply_markup=cancel_keyboard()
    )

def process_withdraw_account(chat_id, text):
    session = get_session(chat_id)
    if not session or session.get("withdraw_step") != "account":
        return False
    session["account"] = text.strip()
    session["withdraw_step"] = "amount"
    set_session(chat_id, session)
    send_message(
        "💰 **Enter the amount** you want to withdraw:",
        chat_id, reply_markup=cancel_keyboard()
    )
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
        send_message("❌ **Enter a valid amount.**", chat_id)
        return False
    uid = str(chat_id)
    if user_balances.get(uid, 0.0) < amount:
        send_message("❌ **Insufficient balance!**", chat_id)
        return False
    w_id = uuid.uuid4().hex[:10]
    withdraw_requests.append({
        "id": w_id, "user_id": chat_id, "amount": amount,
        "method": session["method"], "account": session["account"],
        "status": "pending", "timestamp": time.time()
    })
    deduct_balance(chat_id, amount)
    save_all()
    send_message(
        f"✅ **Withdraw request submitted!**\n"
        f"🆔 **ID:** `{w_id}`\n"
        f"💰 **Amount:** {amount} BDT\n"
        f"📌 Status: ⏳ Pending",
        chat_id, reply_markup=main_keyboard(chat_id)
    )
    send_message(
        f"📥 **New Withdraw Request**\n"
        f"🆔 `{w_id}`\n👤 User: `{chat_id}`\n💰 {amount} BDT\n💳 {session['method'].upper()}\n📞 {session['account']}",
        ADMIN_CHAT_ID
    )
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

        # গ্রুপে অচেনা টেক্সট ইগনোর
        if chat_type != "private":
            return

        if chat_id not in subscribed_users:
            with data_lock:
                subscribed_users.add(chat_id)
                save_all()

        # ===== ADMIN SECTION =====
        if chat_id == ADMIN_CHAT_ID:
            if chat_id in admin_cred_upload_session:
                if text == "/cancel" or text == "❌ Cancel":
                    del admin_cred_upload_session[chat_id]
                    send_message("❌ Cancelled.", chat_id, reply_markup=admin_keyboard())
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
        
        # ===== 2FA BUTTON HANDLER (Reply Keyboard) =====
        if text == "🔐 Enter 2FA":
            prompt_twofa(chat_id)
            return

        # ===== CANCEL & NAVIGATION =====
        if text == "❌ Cancel" or text == "❌ বাতিল করুন":
            clear_session(chat_id)
            send_message("❌ Cancelled.", chat_id, reply_markup=main_keyboard(chat_id))
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
        
        if text == "🚀 Start":
            start_work(chat_id)
            return
        
        # ===== YES/NO FOLLOW =====
        if text == "✅ Yes":
            process_follow_yes(chat_id)
            return
        if text == "❌ No":
            process_follow_no(chat_id)
            return
        
        if text == "🔄 Open Another Account":
            # Clear old session and start new
            clear_session(chat_id)
            instagram_work(chat_id)
            return

        # ===== WITHDRAW METHODS =====
        if text == "💸 bKash":
            withdraw_request(chat_id, "bkash")
            return
        if text == "💸 Nagad":
            withdraw_request(chat_id, "nagad")
            return

        # ===== SESSION STEPS =====
        if session:
            if session.get("withdraw_step") == "account":
                process_withdraw_account(chat_id, text)
                return
            if session.get("withdraw_step") == "amount":
                process_withdraw_amount(chat_id, text)
                return
            if session.get("active") and session.get("step") == "twofa_input":
                process_twofa(chat_id, text)
                return

        # ===== UNKNOWN TEXT =====
        send_message(
            "❌ I didn't understand that. Please use the buttons below.",
            chat_id, reply_markup=main_keyboard(chat_id)
        )

    elif "callback_query" in update:
        cb = update["callback_query"]
        chat_id = str(cb["message"]["chat"]["id"])
        data = cb["data"]
        answer_callback(cb["id"])
        
        # কিছু ইনলাইন বাটন থাকলে (যেমন, অ্যাডমিন পেজিনেশন বা কনফার্ম)
        # আপনি চাইলে এখানে হ্যান্ডেল করতে পারেন, কিন্তু আমরা এখন রিপ্লাই কিবোর্ড ব্যবহার করছি।

# ================== FLASK ==================
@app.route("/")
def home():
    return "🤖 Bot is Running!"

# ================== MAIN ==================
if __name__ == "__main__":
    load_all()
    logger.info(f"Loaded: {len(subscribed_users)} users, {len(credentials)} credentials")

    auto_restore_from_channel()

    threading.Thread(target=auto_backup_loop, daemon=True).start()
    threading.Thread(target=handle_updates, daemon=True).start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)

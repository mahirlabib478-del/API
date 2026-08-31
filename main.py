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

# ================== কনফিগারেশন ==================
BOT_TOKEN = "8692984075:AAEpPTKdqD5dgGGBfYYpLPPyi26U93qVnzI"
ADMIN_CHAT_ID = "8538304896"

# ================== লগিং ==================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================== ফাইল পাথ ==================
USERS_FILE = "users.json"
SESSIONS_FILE = "sessions.json"
CREDENTIALS_FILE = "credentials.json"
WITHDRAWS_FILE = "withdraws.json"
CONFIG_FILE = "config.json"
USER_BALANCES_FILE = "user_balances.json"
USER_INFO_FILE = "user_info.json"

# ================== গ্লোবাল ==================
subscribed_users = set()
user_sessions = {}
credentials = []
withdraw_requests = []
config = {
    "bkash_number": "01XXXXXXXXX",
    "nagad_number": "01XXXXXXXXX",
    "base_balance": 10.0,
    "channel_id": "",
    "work_rules": "📱 **Instagram অ্যাকাউন্ট খোলার নিয়মাবলী**\n\n"
                  "1️⃣ বট আপনাকে একটি ইমেইল ও পাসওয়ার্ড প্রদান করবে।\n"
                  "2️⃣ 2FA কোড দিন (না থাকলে 0 লিখুন)।\n"
                  "3️⃣ ৫টি প্রোফাইল ফলো করুন।\n"
                  "4️⃣ সফলভাবে সম্পন্ন হলে ব্যালেন্স যোগ হবে।"
}
user_balances = {}
user_info = {}

admin_cred_upload_session = {}
admin_cred_list_page = {}
last_backup_message_id = None
last_backup_part_ids = []
save_pending = False
save_timer = None

data_lock = threading.RLock()
backup_lock = threading.Lock()
app = Flask(__name__)

# ================== ফাইল I/O ==================
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
            logger.error(f"সেভ ব্যর্থ {filename}: {e}")

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

# ================== ডেবাউন্সড ব্যাকআপ ==================
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

# ================== টেলিগ্রাম হেল্পার ==================
def send_message(text, chat_id, reply_markup=None, parse_mode="Markdown"):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        return requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"সেন্ড error: {e}")
        return None

def send_document(file_bytes, filename, chat_id, caption=""):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    try:
        files = {'document': (filename, file_bytes, 'application/gzip')}
        return requests.post(url, data={"chat_id": chat_id, "caption": caption}, files=files, timeout=60)
    except Exception as e:
        logger.error(f"ডকুমেন্ট error: {e}")
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
        logger.error(f"এডিট error: {e}")
        return None

# ================== ইন্সটাগ্রাম অ্যাকাউন্ট ক্রিয়েট (সিমুলেটেড) ==================
def create_instagram_account(email, password):
    """
    ইন্সটাগ্রাম অ্যাকাউন্ট তৈরির সিমুলেশন
    আসলে এটি ব্রাউজারের মাধ্যমে ইন্সটাগ্রামে লগইন করে অ্যাকাউন্ট তৈরি করবে
    """
    try:
        # এটি একটি সিমুলেশন - আসলে ইন্সটাগ্রামে অ্যাকাউন্ট তৈরি করা জটিল
        # আপনি চাইলে এখানে Selenium বা Playwright ব্যবহার করতে পারেন
        time.sleep(3)  # নেটওয়ার্ক ডেলি
        
        # সিমুলেটেড সফলতা
        return {
            "success": True,
            "message": f"অ্যাকাউন্ট তৈরি সফল! ইমেইল: {email}"
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"অ্যাকাউন্ট তৈরি ব্যর্থ: {str(e)}"
        }

# ================== ব্যালেন্স ও প্রোফাইল ==================
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

def get_profile_text(user_id):
    uid = str(user_id)
    name = user_info.get(uid, {}).get("name", f"User_{user_id}")
    balance = user_balances.get(uid, 0.0)
    total_accounts = user_info.get(uid, {}).get("total_accounts", 0)
    return (
        f"👤 **ব্যবহারকারী প্রোফাইল**\n\n"
        f"📛 **নাম:** {name}\n"
        f"🆔 **আইডি:** `{user_id}`\n"
        f"💰 **ব্যালেন্স:** `{balance:.2f}` টাকা\n"
        f"📦 **মোট অ্যাকাউন্ট খোলা:** {total_accounts} টি\n"
    )

# ================== কিবোর্ড ==================
def main_keyboard(chat_id):
    kb = [
        ["📱 Instagram Work", "👤 প্রোফাইল"],
        ["💸 উইথড্র"]
    ]
    if str(chat_id) == ADMIN_CHAT_ID:
        kb.append(["⚙️ অ্যাডমিন প্যানেল"])
    return {"keyboard": kb, "resize_keyboard": True}

def admin_keyboard():
    return {
        "keyboard": [
            ["➕ অ্যাকাউন্ট যোগ", "📋 অ্যাকাউন্ট লিস্ট"],
            ["🗑️ অ্যাকাউন্ট ডিলিট", "📊 পরিসংখ্যান"],
            ["💰 ব্যালেন্স সেট", "💲 মূল্য সেট"],
            ["💳 বিকাশ নম্বর সেট", "💳 নগদ নম্বর সেট"],
            ["📝 নিয়ম পরিবর্তন", "📥 উইথড্র রিকোয়েস্ট"],
            ["📁 ব্যাকআপ", "📥 রিস্টোর"],
            ["🔙 মূল মেনু"]
        ],
        "resize_keyboard": True
    }

def work_start_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🚀 Start", "callback_data": "work_start"}],
            [{"text": "❌ Cancel", "callback_data": "work_cancel"}]
        ]
    }

def twofa_button_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🔐 2FA দিন", "callback_data": "twofa_prompt"}],
            [{"text": "❌ বাতিল করুন", "callback_data": "work_cancel"}]
        ]
    }

def yes_no_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "✅ হ্যাঁ, সম্পন্ন", "callback_data": "follow_yes"},
             {"text": "❌ না, এখনো করিনি", "callback_data": "follow_no"}]
        ]
    }

def next_or_cancel():
    return {
        "inline_keyboard": [
            [{"text": "🔄 আরেকটি অ্যাকাউন্ট খুলুন", "callback_data": "work_start"}],
            [{"text": "❌ বন্ধ করুন", "callback_data": "work_cancel"}]
        ]
    }

def cancel_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "❌ বাতিল করুন", "callback_data": "work_cancel"}]
        ]
    }

def withdraw_method_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "💸 বিকাশ", "callback_data": "withdraw_bkash"},
             {"text": "💸 নগদ", "callback_data": "withdraw_nagad"}],
            [{"text": "❌ বাতিল", "callback_data": "withdraw_cancel"}]
        ]
    }

# ================== ক্রেডেনশিয়াল ==================
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

# ================== সেশন ==================
def get_session(chat_id):
    return user_sessions.get(str(chat_id))

def set_session(chat_id, data):
    user_sessions[str(chat_id)] = data
    save_all()

def clear_session(chat_id):
    user_sessions.pop(str(chat_id), None)
    save_all()

# ================== ব্যাকআপ সিস্টেম ==================
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
        logger.error(f"ব্যাকআপ ক্লিনআপ error: {e}")

def save_data_to_channel():
    global last_backup_message_id, last_backup_part_ids
    channel_id = config.get("channel_id")
    if not channel_id:
        logger.warning("ব্যাকআপ চ্যানেল আইডি সেট নেই!")
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
                    logger.error("একক ব্যাকআপ পাঠাতে ব্যর্থ")
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
                        data={"chat_id": channel_id, "caption": f"পার্ট {idx}/{total}"},
                        files={"document": (part_filename, chunk, "application/gzip")},
                        timeout=60
                    )
                    if resp.status_code == 200 and resp.json().get("ok"):
                        part_ids.append(resp.json()["result"]["message_id"])
                    else:
                        logger.error(f"পার্ট {idx} পাঠাতে ব্যর্থ")
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
                    logger.error("ইনডেক্স মেসেজ পাঠাতে ব্যর্থ")
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
                logger.info("ব্যাকআপ সফলভাবে সংরক্ষিত হয়েছে")
        except Exception as e:
            logger.error(f"ব্যাকআপ সেভ error: {e}")

def auto_backup_loop():
    while True:
        time.sleep(300)
        save_data_to_channel()

def auto_restore_from_channel():
    global last_backup_message_id, last_backup_part_ids
    global subscribed_users, credentials, withdraw_requests, config, user_balances, user_info

    channel_id = config.get("channel_id")
    if not channel_id:
        logger.warning("ব্যাকআপ চ্যানেল আইডি সেট নেই")
        return

    try:
        s = requests.Session()
        resp = s.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getChat?chat_id={channel_id}",
            timeout=20
        ).json()
        if not resp.get("ok"):
            logger.error("চ্যানেল অ্যাক্সেস করা যাচ্ছে না")
            return

        pinned = resp["result"].get("pinned_message")
        if not pinned:
            logger.info("পিন করা ব্যাকআপ নেই")
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
                    logger.error(f"পার্ট {part_msg_id} পাওয়া যায়নি")
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

        logger.info(f"ডেটা রিস্টোর করা হয়েছে: {len(subscribed_users)} ইউজার, {len(credentials)} ক্রেডেনশিয়াল")
    except Exception as e:
        logger.error(f"অটো রিস্টোর error: {e}")

# ================== অ্যাডমিন ফাংশন ==================
def admin_panel(chat_id):
    send_message("⚙️ **অ্যাডমিন কন্ট্রোল প্যানেল**", chat_id, reply_markup=admin_keyboard())

def admin_add_creds_prompt(chat_id):
    admin_cred_upload_session[chat_id] = {"step": "email"}
    send_message(
        "📧 **ইমেইল লিস্ট দিন**\n\n"
        "প্রতি লাইনে একটি ইমেইল লিখুন:\n\n"
        "উদাহরণ:\n"
        "`user1@gmail.com`\n"
        "`user2@yahoo.com`\n"
        "`user3@outlook.com`\n\n"
        "বাতিল করতে `/cancel` লিখুন।",
        chat_id,
        reply_markup={"inline_keyboard": [[{"text": "❌ বাতিল", "callback_data": "admin_cancel"}]]}
    )

def process_admin_creds_email(chat_id, text):
    if chat_id not in admin_cred_upload_session or admin_cred_upload_session[chat_id].get("step") != "email":
        return False
    
    emails = [line.strip() for line in text.strip().splitlines() if line.strip()]
    valid_emails = [e for e in emails if '@' in e]
    
    if not valid_emails:
        send_message("❌ **কোনো বৈধ ইমেইল পাওয়া যায়নি।**", chat_id)
        return True
    
    invalid_count = len(emails) - len(valid_emails)
    admin_cred_upload_session[chat_id]["emails"] = valid_emails
    admin_cred_upload_session[chat_id]["step"] = "password"
    
    msg = f"✅ **{len(valid_emails)}** টি ইমেইল গ্রহণ করা হয়েছে।"
    if invalid_count > 0:
        msg += f"\n⚠️ {invalid_count} টি ইনভ্যালিড ইমেইল বাদ পড়েছে।"
    
    msg += "\n\n🔑 **এখন পাসওয়ার্ড দিন** (সব অ্যাকাউন্টের জন্য একটি পাসওয়ার্ড):"
    send_message(msg, chat_id)
    return True

def process_admin_creds_password(chat_id, text):
    if chat_id not in admin_cred_upload_session or admin_cred_upload_session[chat_id].get("step") != "password":
        return False
    
    password = text.strip()
    if not password:
        send_message("❌ **পাসওয়ার্ড খালি রাখা যাবে না।**", chat_id)
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
        f"✅ **{added}** টি অ্যাকাউন্ট যোগ করা হয়েছে।\n"
        f"📦 **মোট:** {len(credentials)} টি ক্রেডেনশিয়াল",
        chat_id, reply_markup=admin_keyboard()
    )
    return True

def admin_list_creds(chat_id, page=0, message_id=None):
    total = len(credentials)
    if total == 0:
        send_message("📭 **কোনো ক্রেডেনশিয়াল নেই।**", chat_id, reply_markup=admin_keyboard())
        return

    per_page = 10
    total_pages = (total + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = min(start + per_page, total)
    page_items = credentials[start:end]

    lines = [f"📋 **ক্রেডেনশিয়াল লিস্ট** (পৃষ্ঠা {page+1}/{total_pages})\n"]
    lines.append("ইমেইল | পাসওয়ার্ড | স্ট্যাটাস\n")
    lines.append("---|---|---\n")

    for idx, cred in enumerate(page_items, start=start):
        status = "🟢 অব্যবহৃত" if not cred.get("used", False) else f"🔴 ব্যবহৃত ({cred.get('assigned_to', 'N/A')})"
        lines.append(f"`{cred['email']}` | `{cred['password']}` | {status}")

    text = "\n".join(lines)
    kb = {"inline_keyboard": []}

    nav = []
    if page > 0:
        nav.append({"text": "⬅️ আগের", "callback_data": f"credpage_{page-1}"})
    if page < total_pages - 1:
        nav.append({"text": "➡️ পরের", "callback_data": f"credpage_{page+1}"})
    if nav:
        kb["inline_keyboard"].append(nav)

    for i, cred in enumerate(page_items, start=start):
        kb["inline_keyboard"].append([
            {"text": f"🗑️ {i+1}. {cred['email']}", "callback_data": f"delcred_{i}"}
        ])

    kb["inline_keyboard"].append([{"text": "🔙 বন্ধ করুন", "callback_data": "close_credlist"}])

    if message_id:
        edit_message_text(chat_id, message_id, text, reply_markup=kb)
    else:
        send_message(text, chat_id, reply_markup=kb)

def admin_delete_single_cred(chat_id, index, message_id):
    deleted = delete_credential_by_index(index)
    if deleted:
        send_message(f"🗑️ `{deleted['email']}` ডিলিট করা হয়েছে।", chat_id)
        admin_list_creds(chat_id, page=0, message_id=message_id)
    else:
        send_message("❌ ক্রেডেনশিয়াল পাওয়া যায়নি।", chat_id)

def admin_delete_all_creds(chat_id):
    with data_lock:
        count = len(credentials)
        credentials.clear()
        save_all()
    send_message(f"🗑️ **{count}** টি সকল ক্রেডেনশিয়াল ডিলিট করা হয়েছে।", chat_id, reply_markup=admin_keyboard())

def admin_stats(chat_id):
    total_creds = len(credentials)
    used_creds = len([c for c in credentials if c.get("used", False)])
    pending_withdraw = len([w for w in withdraw_requests if w["status"] == "pending"])
    send_message(
        f"📊 **পরিসংখ্যান**\n\n"
        f"👥 **মোট ইউজার:** {len(subscribed_users)}\n"
        f"📦 **ক্রেডেনশিয়াল:** {total_creds} টি (ব্যবহৃত: {used_creds})\n"
        f"💸 **পেন্ডিং উইথড্র:** {pending_withdraw} টি\n"
        f"💰 **বর্তমান মূল্য:** {config.get('base_balance', 10.0)} টাকা প্রতি অ্যাকাউন্ট",
        chat_id, parse_mode="Markdown", reply_markup=admin_keyboard()
    )

def admin_show_withdraw_requests(chat_id):
    pending = [w for w in withdraw_requests if w["status"] == "pending"]
    if not pending:
        send_message("📭 **কোনো পেন্ডিং উইথড্র রিকোয়েস্ট নেই।**", chat_id, reply_markup=admin_keyboard())
        return
    msg = "📥 **পেন্ডিং উইথড্র রিকোয়েস্ট**\n\n"
    for w in pending:
        msg += f"🆔 `{w['id']}` | 👤 `{w['user_id']}` | 💰 `{w['amount']}` | 💳 {w['method'].upper()}\n"
    send_message(msg, chat_id, parse_mode="Markdown")

def admin_approve_withdraw(chat_id, w_id):
    with data_lock:
        for w in withdraw_requests:
            if w["id"] == w_id and w["status"] == "pending":
                w["status"] = "approved"
                save_all()
                send_message(f"✅ **উইথড্র {w_id}** অনুমোদিত হয়েছে।", chat_id, reply_markup=admin_keyboard())
                send_message(
                    f"✅ আপনার **{w['amount']}** টাকা উইথড্র অনুমোদন করা হয়েছে।\n"
                    f"আপনার {w['method'].upper()} অ্যাকাউন্টে টাকা পাঠানো হবে।",
                    w["user_id"]
                )
                return
    send_message(f"❌ {w_id} পাওয়া যায়নি।", chat_id)

def admin_reject_withdraw(chat_id, w_id):
    with data_lock:
        for w in withdraw_requests:
            if w["id"] == w_id and w["status"] == "pending":
                w["status"] = "rejected"
                add_balance(w["user_id"], w["amount"])
                save_all()
                send_message(f"❌ **উইথড্র {w_id}** বাতিল করা হয়েছে।", chat_id, reply_markup=admin_keyboard())
                send_message(
                    f"❌ আপনার উইথড্র রিকোয়েস্ট বাতিল করা হয়েছে।\n"
                    f"**{w['amount']}** টাকা আপনার ব্যালেন্সে ফেরত দেয়া হয়েছে।",
                    w["user_id"]
                )
                return
    send_message(f"❌ {w_id} পাওয়া যায়নি।", chat_id)

def admin_set_balance(chat_id, text):
    parts = text.split()
    if len(parts) != 3:
        send_message("❌ **ফরম্যাট:** `/setbalance <user_id> <amount>`", chat_id)
        return
    try:
        user_id, amount = parts[1], float(parts[2])
        user_balances[user_id] = amount
        save_json(USER_BALANCES_FILE, user_balances)
        trigger_backup()
        send_message(f"✅ ইউজার `{user_id}` এর ব্যালেন্স **{amount}** টাকা সেট করা হয়েছে।", chat_id, reply_markup=admin_keyboard())
    except:
        send_message("❌ ভুল ফরম্যাট।", chat_id)

def admin_set_price(chat_id, text):
    parts = text.split()
    if len(parts) != 2:
        send_message("❌ **ফরম্যাট:** `/setprice <amount>`", chat_id)
        return
    try:
        price = float(parts[1])
        if price <= 0:
            raise ValueError
        config["base_balance"] = price
        save_all()
        send_message(f"✅ প্রতি অ্যাকাউন্ট খোলার ইনকাম **{price}** টাকা সেট করা হয়েছে।", chat_id, reply_markup=admin_keyboard())
    except:
        send_message("❌ সঠিক টাকার পরিমাণ দিন।", chat_id)

def admin_set_bkash(chat_id, text):
    parts = text.split()
    if len(parts) != 2:
        send_message("❌ **ফরম্যাট:** `/setbkash <নম্বর>`", chat_id)
        return
    config["bkash_number"] = parts[1]
    save_all()
    send_message(f"✅ বিকাশ নম্বর **{parts[1]}** সেট করা হয়েছে।", chat_id, reply_markup=admin_keyboard())

def admin_set_nagad(chat_id, text):
    parts = text.split()
    if len(parts) != 2:
        send_message("❌ **ফরম্যাট:** `/setnagad <নম্বর>`", chat_id)
        return
    config["nagad_number"] = parts[1]
    save_all()
    send_message(f"✅ নগদ নম্বর **{parts[1]}** সেট করা হয়েছে।", chat_id, reply_markup=admin_keyboard())

def admin_set_rules(chat_id, text):
    parts = text.split(" ", 1)
    if len(parts) != 2:
        send_message("❌ **ফরম্যাট:** `/setrules <নতুন নিয়ম>`", chat_id)
        return
    config["work_rules"] = parts[1]
    save_all()
    send_message("✅ **নিয়মাবলী আপডেট করা হয়েছে।**", chat_id, reply_markup=admin_keyboard())

def admin_set_channel(chat_id, text):
    parts = text.split()
    if len(parts) != 2:
        send_message("❌ **ফরম্যাট:** `/setchannel <চ্যানেল_আইডি>`", chat_id)
        return
    config["channel_id"] = parts[1]
    save_all()
    send_message(f"✅ চ্যানেল আইডি **{parts[1]}** সেট করা হয়েছে।", chat_id, reply_markup=admin_keyboard())

def admin_backup(chat_id):
    send_message("⏳ **ব্যাকআপ নেওয়া হচ্ছে...**", chat_id)
    save_data_to_channel()
    send_message("✅ **ব্যাকআপ সম্পন্ন!**", chat_id, reply_markup=admin_keyboard())

def admin_restore(chat_id):
    send_message("⏳ **রিস্টোর করা হচ্ছে...**", chat_id)
    auto_restore_from_channel()
    send_message("✅ **রিস্টোর সম্পন্ন!**", chat_id, reply_markup=admin_keyboard())

# ================== ইউজার কমান্ড ==================
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
            "🤖 **Instagram অ্যাকাউন্ট খোলার বটে স্বাগতম!**\n\n"
            "আমি আপনাকে ইনস্টাগ্রাম অ্যাকাউন্ট খুলতে সাহায্য করি।\n"
            "নিচের বাটন ব্যবহার করে শুরু করুন।",
            chat_id, reply_markup=main_keyboard(chat_id)
        )
    else:
        send_message(
            "🤖 **Instagram অ্যাকাউন্ট খোলার বট**\n\n"
            "আমাকে প্রাইভেট চ্যাটে `/start` দিন অথবা @username দিয়ে সার্চ করুন।",
            chat_id
        )

def instagram_work(chat_id):
    if get_session(chat_id) and get_session(chat_id).get("active"):
        send_message("⏳ **আপনার একটি চলমান সেশন আছে।** আগে সেটি শেষ করুন বা বাতিল করুন।", chat_id)
        return
    rules = config.get("work_rules", "📱 Instagram অ্যাকাউন্ট খোলার নিয়মাবলী")
    send_message(
        f"{rules}\n\n"
        f"👇 **Start** বাটনে ক্লিক করে শুরু করুন।",
        chat_id, reply_markup=work_start_keyboard()
    )

def start_work(chat_id):
    cred = get_available_credential()
    if not cred:
        send_message("❌ **কোনো অব্যবহৃত অ্যাকাউন্ট নেই!**\nঅ্যাডমিনের সাথে যোগাযোগ করুন।", chat_id, reply_markup=main_keyboard(chat_id))
        return
    assign_credential_to_user(cred, chat_id)
    set_session(chat_id, {
        "active": True, 
        "step": "credentials_shown", 
        "email": cred["email"], 
        "password": cred["password"],
        "twofa": None
    })
    
    # অ্যাকাউন্ট তৈরি করার চেষ্টা
    result = create_instagram_account(cred["email"], cred["password"])
    
    if result["success"]:
        send_message(
            f"✅ **আপনার জন্য একটি অ্যাকাউন্ট বরাদ্দ করা হয়েছে:**\n\n"
            f"📧 **ইমেইল:** `{cred['email']}`\n"
            f"🔑 **পাসওয়ার্ড:** `{cred['password']}`\n\n"
            f"🔄 অ্যাকাউন্ট তৈরি হচ্ছে...\n\n"
            f"🔐 **2FA দিন** বাটনে ক্লিক করে 2FA কোড দিন (যদি থাকে):",
            chat_id, reply_markup=twofa_button_keyboard()
        )
    else:
        send_message(
            f"❌ অ্যাকাউন্ট তৈরি ব্যর্থ!\n{result['message']}\n\n"
            f"আবার চেষ্টা করতে **Start** বাটনে ক্লিক করুন।",
            chat_id, reply_markup=work_start_keyboard()
        )

def prompt_twofa(chat_id):
    session = get_session(chat_id)
    if not session or not session.get("active"):
        return
    
    # 2FA ইনপুট নেওয়ার জন্য স্টেপ পরিবর্তন
    session["step"] = "twofa_input"
    set_session(chat_id, session)
    
    send_message(
        "🔐 **2FA কোড দিন:**\n\n"
        "আপনার ইমেইলে আসা 6-ডিজিটের কোডটি লিখুন।\n"
        "যদি 2FA না থাকে তাহলে `0` লিখুন।",
        chat_id, reply_markup=cancel_keyboard()
    )

def process_twofa(chat_id, text):
    session = get_session(chat_id)
    if not session or not session.get("active") or session.get("step") != "twofa_input":
        return False
    
    twofa_code = text.strip()
    session["twofa"] = twofa_code
    
    # 2FA ভেরিফাই (সিমুলেট)
    if twofa_code == "0":
        pass
    else:
        time.sleep(1)
    
    session["step"] = "follow"
    set_session(chat_id, session)
    
    send_message(
        "✅ **2FA প্রক্রিয়া সম্পন্ন!**\n\n"
        "এখন **৫টি প্রোফাইল ফলো** করুন।\n"
        "প্রত্যেকটি প্রোফাইলের ছবি দিয়ে ফলো করেছেন?",
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
        f"🎉 **অভিনন্দন! আপনার অ্যাকাউন্ট খোলা সম্পন্ন হয়েছে!**\n\n"
        f"📧 **ইমেইল:** `{session['email']}`\n"
        f"🔑 **পাসওয়ার্ড:** `{session['password']}`\n"
        f"🔐 **2FA:** {session.get('twofa', 'N/A')}\n\n"
        f"💰 **{price}** টাকা আপনার ব্যালেন্সে যোগ হয়েছে।\n\n"
        f"আবার নতুন অ্যাকাউন্ট খুলতে নিচের বাটন চাপুন।",
        chat_id, reply_markup=next_or_cancel()
    )
    clear_session(chat_id)

def process_follow_no(chat_id):
    send_message(
        "⚠️ **দয়া করে ৫টি প্রোফাইল ফলো করুন** এবং তারপর **হ্যাঁ** বাটন চাপুন।",
        chat_id, reply_markup=yes_no_keyboard()
    )

def withdraw_start(chat_id):
    clear_session(chat_id)
    send_message(
        "💸 **উইথড্র করুন**\n\n"
        "আপনার টাকা উত্তোলনের মাধ্যম নির্বাচন করুন:",
        chat_id, reply_markup=withdraw_method_keyboard()
    )

def withdraw_request(chat_id, method):
    set_session(chat_id, {"withdraw_step": "account", "method": method})
    send_message(
        f"📞 আপনার {method.upper()} অ্যাকাউন্ট নম্বর দিন:",
        chat_id, reply_markup={"inline_keyboard": [[{"text": "❌ বাতিল", "callback_data": "withdraw_cancel"}]]}
    )

def process_withdraw_account(chat_id, text):
    session = get_session(chat_id)
    if not session or session.get("withdraw_step") != "account":
        return False
    session["account"] = text.strip()
    session["withdraw_step"] = "amount"
    set_session(chat_id, session)
    send_message(
        "💰 **কত টাকা উইথড্র করতে চান?**\n(শুধু সংখ্যা লিখুন)",
        chat_id, reply_markup={"inline_keyboard": [[{"text": "❌ বাতিল", "callback_data": "withdraw_cancel"}]]}
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
        send_message("❌ **সঠিক টাকার পরিমাণ দিন।**", chat_id)
        return False
    uid = str(chat_id)
    if user_balances.get(uid, 0.0) < amount:
        send_message("❌ **অপর্যাপ্ত ব্যালেন্স!**", chat_id)
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
        f"✅ **উইথড্র রিকোয়েস্ট জমা হয়েছে!**\n"
        f"🆔 **আইডি:** `{w_id}`\n"
        f"💰 **পরিমাণ:** {amount} টাকা\n"
        f"📌 স্ট্যাটাস: ⏳ পেন্ডিং\n\n"
        f"অ্যাডমিন অনুমোদন দিলে টাকা পাঠানো হবে।",
        chat_id, reply_markup=main_keyboard(chat_id)
    )
    send_message(
        f"📥 **নতুন উইথড্র রিকোয়েস্ট**\n\n"
        f"🆔 `{w_id}`\n"
        f"👤 ইউজার: `{chat_id}`\n"
        f"💰 {amount} টাকা\n"
        f"💳 {session['method'].upper()}\n"
        f"📞 {session['account']}",
        ADMIN_CHAT_ID
    )
    clear_session(chat_id)
    return True

# ================== আপডেট হ্যান্ডলার ==================
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
            logger.error(f"আপডেট লুপ error: {e}")
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

        # ===== অ্যাডমিন =====
        if chat_id == ADMIN_CHAT_ID:
            if chat_id in admin_cred_upload_session:
                if text == "/cancel":
                    del admin_cred_upload_session[chat_id]
                    send_message("❌ বাতিল করা হয়েছে।", chat_id, reply_markup=admin_keyboard())
                    return
                
                step = admin_cred_upload_session[chat_id].get("step")
                if step == "email":
                    process_admin_creds_email(chat_id, text)
                elif step == "password":
                    process_admin_creds_password(chat_id, text)
                return

            if text == "⚙️ অ্যাডমিন প্যানেল":
                admin_panel(chat_id)
                return
            if text == "➕ অ্যাকাউন্ট যোগ":
                admin_add_creds_prompt(chat_id)
                return
            if text == "📋 অ্যাকাউন্ট লিস্ট":
                admin_list_creds(chat_id)
                return
            if text == "🗑️ অ্যাকাউন্ট ডিলিট":
                admin_delete_all_creds(chat_id)
                return
            if text == "📊 পরিসংখ্যান":
                admin_stats(chat_id)
                return
            if text == "📥 উইথড্র রিকোয়েস্ট":
                admin_show_withdraw_requests(chat_id)
                return
            if text == "📁 ব্যাকআপ":
                admin_backup(chat_id)
                return
            if text == "📥 রিস্টোর":
                admin_restore(chat_id)
                return
            if text == "💰 ব্যালেন্স সেট":
                send_message("/setbalance <user_id> <amount>", chat_id, reply_markup=admin_keyboard())
                return
            if text == "💲 মূল্য সেট":
                send_message("/setprice <amount>", chat_id, reply_markup=admin_keyboard())
                return
            if text == "💳 বিকাশ নম্বর সেট":
                send_message("/setbkash <নম্বর>", chat_id, reply_markup=admin_keyboard())
                return
            if text == "💳 নগদ নম্বর সেট":
                send_message("/setnagad <নম্বর>", chat_id, reply_markup=admin_keyboard())
                return
            if text == "📝 নিয়ম পরিবর্তন":
                send_message("/setrules <নতুন নিয়ম>", chat_id, reply_markup=admin_keyboard())
                return
            if text == "🔙 মূল মেনু":
                send_message("🔙 মূল মেনুতে ফিরে যান।", chat_id, reply_markup=main_keyboard(chat_id))
                return

            if text.startswith("/setbalance"):
                admin_set_balance(chat_id, text)
                return
            if text.startswith("/setprice"):
                admin_set_price(chat_id, text)
                return
            if text.startswith("/setbkash"):
                admin_set_bkash(chat_id, text)
                return
            if text.startswith("/setnagad"):
                admin_set_nagad(chat_id, text)
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

        # ===== ইউজার =====
        if text == "📱 Instagram Work":
            instagram_work(chat_id)
            return
        if text == "👤 প্রোফাইল":
            send_message(get_profile_text(chat_id), chat_id)
            return
        if text == "💸 উইথড্র":
            withdraw_start(chat_id)
            return

        # ===== সেশন =====
        session = get_session(chat_id)
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

        send_message(
            "❌ আমি বুঝতে পারিনি।\n"
            "দয়া করে /start দিন অথবা নিচের বাটন ব্যবহার করুন।",
            chat_id, reply_markup=main_keyboard(chat_id)
        )

    elif "callback_query" in update:
        cb = update["callback_query"]
        chat_id = str(cb["message"]["chat"]["id"])
        data = cb["data"]
        message_id = cb["message"]["message_id"]
        answer_callback(cb["id"])

        if data == "work_start":
            start_work(chat_id)
        elif data == "work_cancel":
            clear_session(chat_id)
            send_message("❌ প্রক্রিয়া বাতিল করা হয়েছে।", chat_id, reply_markup=main_keyboard(chat_id))
        elif data == "twofa_prompt":
            prompt_twofa(chat_id)
        elif data == "follow_yes":
            process_follow_yes(chat_id)
        elif data == "follow_no":
            process_follow_no(chat_id)
        elif data == "admin_cancel":
            if chat_id in admin_cred_upload_session:
                del admin_cred_upload_session[chat_id]
            send_message("❌ বাতিল করা হয়েছে।", chat_id, reply_markup=admin_keyboard())
        elif data == "withdraw_bkash":
            withdraw_request(chat_id, "bkash")
        elif data == "withdraw_nagad":
            withdraw_request(chat_id, "nagad")
        elif data == "withdraw_cancel":
            clear_session(chat_id)
            send_message("❌ উইথড্র বাতিল করা হয়েছে।", chat_id, reply_markup=main_keyboard(chat_id))
        elif data.startswith("credpage_"):
            page = int(data.split("_")[1])
            admin_list_creds(chat_id, page=page, message_id=message_id)
        elif data.startswith("delcred_"):
            index = int(data.split("_")[1])
            admin_delete_single_cred(chat_id, index, message_id)
        elif data == "close_credlist":
            delete_message(chat_id, message_id)
            send_message("📋 তালিকা বন্ধ করা হয়েছে।", chat_id, reply_markup=admin_keyboard())

# ================== ফ্লাস্ক ==================
@app.route("/")
def home():
    return "🤖 Bot is Running!"

# ================== মেইন ==================
if __name__ == "__main__":
    load_all()
    logger.info(f"লোড হয়েছে: {len(subscribed_users)} ইউজার, {len(credentials)} ক্রেডেনশিয়াল")

    auto_restore_from_channel()

    threading.Thread(target=auto_backup_loop, daemon=True).start()
    threading.Thread(target=handle_updates, daemon=True).start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)

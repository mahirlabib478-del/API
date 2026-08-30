import os
import time
import json
import threading
import logging
import uuid
import requests
from flask import Flask
from datetime import datetime

# ================== কনফিগারেশন ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8692984075:AAFjiQ4aj1YZi8sSCLSGeVsnY0FMOQM2Onw")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "8538304896")
API_INFO_URL = "https://skysysx.net/api/info"      # স্ট্যাটাস চেকের URL
API_SUBMIT_URL = "http://skysysx.net/e/boss"       # কুকি সাবমিটের URL (আগের মতো)

if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
     raise RuntimeError("BOT_TOKEN সেট করুন!")

# ================== লগিং ==================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================== ফাইল পাথ ==================
USERS_FILE = "users.json"
PENDING_FILE = "pending_cookies.json"
CONFIG_FILE = "config.json"

# ================== গ্লোবাল ভেরিয়েবল ==================
pending_cookies = []
subscribed_users = set()
api_status = {
    "online": False,
    "offline_locked": True,
    "webhook_status": "unknown",
    "push_blocked": False,
    "push_locked": False,
    "daily": {
        "accounts_pushed": 0,
        "accounts_success": 0,
        "accounts_failed": 0,
        "date": ""
    },
    "staging_count": 0,
    "staging_drip_rate": 0,
    "staging_auto_release": False
}
last_check_time = 0
config = {
    "check_interval": 30,
    "maintenance": False
}

data_lock = threading.RLock()
app = Flask(__name__)

# ================== ফাইল I/O ==================
def load_json(filename, default):
    try:
        with open(filename, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json(filename, data):
    with data_lock:
        try:
            with open(filename, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"সেভ করতে ব্যর্থ {filename}: {e}")

def load_all():
    global pending_cookies, subscribed_users, config
    pending_cookies = load_json(PENDING_FILE, [])
    subscribed_users = set(load_json(USERS_FILE, []))
    cfg = load_json(CONFIG_FILE, {})
    for k in config:
        if k not in cfg:
            cfg[k] = config[k]
    config = cfg

def save_all():
    save_json(PENDING_FILE, pending_cookies)
    save_json(USERS_FILE, list(subscribed_users))
    save_json(CONFIG_FILE, config)

# ================== টেলিগ্রাম হেল্পার ==================
def send_telegram_message(text, chat_id, reply_markup=None, parse_mode=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp
    except Exception as e:
        logger.error(f"সেন্ড error: {e}")
        return None

def broadcast_message(text):
    users = list(subscribed_users)
    for uid in users:
        try:
            send_telegram_message(text, uid)
            time.sleep(0.05)
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

# ================== কিবোর্ড ==================
def get_main_keyboard(chat_id):
    kb = [
        ["📤 কুকি সাবমিট করুন"],
        ["📊 API স্ট্যাটাস", "⏳ পেন্ডিং দেখুন"]
    ]
    if str(chat_id) == ADMIN_CHAT_ID:
        kb.append(["⚙️ অ্যাডমিন প্যানেল"])
    return {"keyboard": kb, "resize_keyboard": True}

def admin_keyboard():
    return {
        "keyboard": [
            ["📊 সার্বিক পরিসংখ্যান", "📤 ফোর্স পুশ"],
            ["🗑️ পেন্ডিং ক্লিয়ার", "🔧 মেইন্টেনেন্স টগল"],
            ["🔙 মূল মেনু"]
        ],
        "resize_keyboard": True
    }

# ================== API ইন্টিগ্রেশন (আপডেটেড) ==================
def fetch_api_status():
    """API থেকে রিয়েল টাইম স্ট্যাটাস নিয়ে আসে"""
    global api_status, last_check_time
    try:
        resp = requests.get(API_INFO_URL, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            # API অনলাইন কিনা: offline_locked false হলে এবং webhook_status fail না হলে
            is_online = not data.get("api_offline_locked", True) and data.get("webhook_status") != "fail"
            
            api_status = {
                "online": is_online,
                "offline_locked": data.get("api_offline_locked", True),
                "webhook_status": data.get("webhook_status", "unknown"),
                "push_blocked": data.get("push_blocked", False),
                "push_locked": data.get("push_locked", False),
                "daily": data.get("daily", {
                    "accounts_pushed": 0,
                    "accounts_success": 0,
                    "accounts_failed": 0,
                    "date": ""
                }),
                "staging_count": data.get("staging_count", 0),
                "staging_drip_rate": data.get("staging_drip_rate", 0),
                "staging_auto_release": data.get("staging_auto_release", False)
            }
            last_check_time = time.time()
            return api_status
        else:
            logger.warning(f"API রেসপন্স: {resp.status_code}")
            api_status["online"] = False
            return api_status
    except Exception as e:
        logger.error(f"API চেক error: {e}")
        api_status["online"] = False
        return api_status

def check_api_status():
    """শুধু অনলাইন/অফলাইন স্ট্যাটাস রিটার্ন করে"""
    status = fetch_api_status()
    return status["online"]

def submit_cookie_to_api(cookie_text):
    """API-তে কুকি পাঠায় (আগের মতোই)"""
    try:
        payload = {"cookies": cookie_text}
        headers = {"Content-Type": "application/json"}
        resp = requests.post(API_SUBMIT_URL, json=payload, headers=headers, timeout=15)
        if 200 <= resp.status_code < 300:
            return True
        else:
            logger.warning(f"API রেসপন্স: {resp.status_code} - {resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"API পুশ error: {e}")
        return False

# ================== কুকি হ্যান্ডলিং ==================
def add_to_queue(user_id, cookie_text):
    with data_lock:
        pending_cookies.append({
            "id": uuid.uuid4().hex[:8],
            "user_id": str(user_id),
            "cookies": cookie_text,
            "timestamp": time.time()
        })
        save_all()
    logger.info(f"কিউতে যোগ: ইউজার {user_id}")

def process_pending_queue():
    global pending_cookies
    if not pending_cookies:
        return

    with data_lock:
        to_process = pending_cookies.copy()
        pending_cookies = []
        save_all()

    success_count = 0
    fail_count = 0
    for item in to_process:
        user_id = item["user_id"]
        cookie = item["cookies"]
        if submit_cookie_to_api(cookie):
            success_count += 1
            send_telegram_message(
                f"✅ আপনার আগের জমা দেওয়া কুকি এখন API-তে সফলভাবে পুশ করা হয়েছে (আইডি: {item['id']})।",
                user_id
            )
        else:
            fail_count += 1
            with data_lock:
                pending_cookies.append(item)
                save_all()

    if success_count > 0:
        logger.info(f"{success_count} টি কুকি পুশ সফল")
    if fail_count > 0:
        logger.warning(f"{fail_count} টি কুকি পুশ ব্যর্থ, কিউতে ফেরত রাখা হলো")

def handle_cookie_submission(chat_id, cookie_text):
    if config.get("maintenance", False) and str(chat_id) != ADMIN_CHAT_ID:
        send_telegram_message("🔧 বর্তমানে রক্ষণাবেক্ষণ চলছে। পরে চেষ্টা করুন।", chat_id)
        return

    if not cookie_text or len(cookie_text.strip()) < 5:
        send_telegram_message("❌ সঠিক কুকি টেক্সট দিন। খুব ছোট দেখাচ্ছে।", chat_id)
        return

    # রিয়েল টাইম স্ট্যাটাস fetch
    status = fetch_api_status()
    
    if status["online"]:
        success = submit_cookie_to_api(cookie_text.strip())
        if success:
            send_telegram_message("✅ আপনার কুকি সফলভাবে API-তে পুশ করা হয়েছে। ধন্যবাদ!", chat_id)
            logger.info(f"ইউজার {chat_id} থেকে কুকি পুশ সফল")
        else:
            add_to_queue(chat_id, cookie_text.strip())
            send_telegram_message("⚠️ API অনলাইন থাকলেও পুশ করতে ব্যর্থ। কিউতে রাখা হলো।", chat_id)
    else:
        add_to_queue(chat_id, cookie_text.strip())
        # অফলাইনের কারণ জানানো
        reason = ""
        if status["offline_locked"]:
            reason = "API অফলাইন লকড"
        elif status["webhook_status"] == "fail":
            reason = "ওয়েবহুক ব্যর্থ"
        else:
            reason = "অজানা কারণে অফলাইন"
        send_telegram_message(
            f"⏳ বায়ার API বর্তমানে অফলাইন।\nকারণ: {reason}\nআপনার কুকি কিউতে সংরক্ষণ করা হয়েছে। API অনলাইন হলে অটো পুশ হবে।",
            chat_id
        )

# ================== ব্যাকগ্রাউন্ড মনিটর থ্রেড ==================
previous_online_status = None

def api_monitor_loop():
    global previous_online_status
    while True:
        try:
            current_status = fetch_api_status()
            is_online = current_status["online"]
            
            # স্ট্যাটাস পরিবর্তন হলে ব্রডকাস্ট
            if previous_online_status is None:
                previous_online_status = is_online
            elif is_online != previous_online_status:
                previous_online_status = is_online
                status_text = "🟢 অনলাইন" if is_online else "🔴 অফলাইন"
                broadcast_message(f"📢 বায়ার API স্ট্যাটাস পরিবর্তন: এখন {status_text}")
                logger.info(f"API স্ট্যাটাস পরিবর্তন: {status_text}")

            # অনলাইন হলে কিউ প্রসেস
            if is_online and pending_cookies:
                process_pending_queue()

            time.sleep(config.get("check_interval", 30))
        except Exception as e:
            logger.error(f"মনিটর লুপ error: {e}")
            time.sleep(60)

# ================== স্ট্যাটাস মেসেজ (আপডেটেড) ==================
def get_status_text():
    """API স্ট্যাটাসের বিস্তারিত টেক্সট তৈরি করে"""
    s = api_status
    status_emoji = "🟢" if s["online"] else "🔴"
    status_text = "অনলাইন" if s["online"] else "অফলাইন"
    
    lines = [
        f"📊 **API স্ট্যাটাস**\n",
        f"🌐 অবস্থা: {status_emoji} {status_text}",
        f"🔒 অফলাইন লক: {'হ্যাঁ' if s['offline_locked'] else 'না'}",
        f"📡 ওয়েবহুক: {s['webhook_status']}",
        f"🚫 পুশ ব্লক: {'হ্যাঁ' if s['push_blocked'] else 'না'}",
        f"🔐 পুশ লক: {'হ্যাঁ' if s['push_locked'] else 'না'}",
        f"",
        f"📅 **আজকের পরিসংখ্যান** ({s['daily'].get('date', 'N/A')})",
        f"📤 মোট পুশ: {s['daily'].get('accounts_pushed', 0)}",
        f"✅ সফল: {s['daily'].get('accounts_success', 0)}",
        f"❌ ব্যর্থ: {s['daily'].get('accounts_failed', 0)}",
    ]
    
    if s.get('staging_count', 0) > 0:
        lines.append(f"\n📦 স্টেজিং কাউন্ট: {s['staging_count']}")
        lines.append(f"💧 ড্রিপ রেট: {s['staging_drip_rate']}")
        lines.append(f"🔄 অটো রিলিজ: {'চালু' if s['staging_auto_release'] else 'বন্ধ'}")
    
    lines.append(f"\n⏳ পেন্ডিং কুকি: {len(pending_cookies)} টি")
    lines.append(f"🕒 শেষ চেক: {datetime.fromtimestamp(last_check_time).strftime('%H:%M:%S')}")
    
    return "\n".join(lines)

# ================== কমান্ড হ্যান্ডলার ==================
def handle_start(chat_id):
    with data_lock:
        subscribed_users.add(str(chat_id))
        save_all()
    send_telegram_message(
        "🚀 স্বাগতম! এই বট Instagram কুকি API-তে পুশ করার জন্য।\n\n"
        "📤 'কুকি সাবমিট করুন' বাটনে চেপে কুকি পাঠান।\n"
        "📊 'API স্ট্যাটাস' দেখে API অনলাইন/অফলাইন ও পরিসংখ্যান জানুন।",
        chat_id,
        reply_markup=get_main_keyboard(chat_id)
    )

def handle_status(chat_id):
    fetch_api_status()  # রিফ্রেশ
    text = get_status_text()
    send_telegram_message(text, chat_id, parse_mode="Markdown")

def handle_pending_list(chat_id):
    if not pending_cookies:
        send_telegram_message("📭 কোনো পেন্ডিং কুকি নেই।", chat_id)
        return
    items = pending_cookies[:5]
    lines = ["⏳ **পেন্ডিং কুকি লিস্ট** (সর্বশেষ ৫টি):\n"]
    for item in items:
        user = item["user_id"]
        time_str = datetime.fromtimestamp(item["timestamp"]).strftime("%d/%m %H:%M")
        lines.append(f"🆔 {item['id']} | ইউজার: {user} | {time_str}")
    if len(pending_cookies) > 5:
        lines.append(f"\n... এবং আরও {len(pending_cookies)-5} টি।")
    send_telegram_message("\n".join(lines), chat_id, parse_mode="Markdown")

# ================== অ্যাডমিন কমান্ড ==================
def admin_stats(chat_id):
    fetch_api_status()
    text = get_status_text()
    text += f"\n\n👥 মোট ইউজার: {len(subscribed_users)} জন"
    text += f"\n🔧 মেইন্টেনেন্স: {'🔒 চালু' if config.get('maintenance') else '🔓 বন্ধ'}"
    send_telegram_message(text, chat_id, parse_mode="Markdown", reply_markup=admin_keyboard())

def force_push(chat_id):
    if not pending_cookies:
        send_telegram_message("📭 পুশ করার মতো কোনো পেন্ডিং কুকি নেই।", chat_id)
        return
    send_telegram_message("⏳ ফোর্স পুশ শুরু হচ্ছে...", chat_id)
    threading.Thread(target=process_pending_queue, daemon=True).start()

def clear_pending(chat_id):
    with data_lock:
        pending_cookies.clear()
        save_all()
    send_telegram_message("🗑️ সব পেন্ডিং কুকি ক্লিয়ার করা হয়েছে।", chat_id)

def toggle_maintenance(chat_id):
    current = config.get("maintenance", False)
    config["maintenance"] = not current
    save_all()
    status = "চালু" if config["maintenance"] else "বন্ধ"
    send_telegram_message(f"🔧 মেইন্টেনেন্স মোড {status} করা হয়েছে।", chat_id)

# ================== মেসেজ হ্যান্ডলার ==================
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
    chat_id = None
    chat_type = "private"
    text = ""

    if "message" in update:
        msg = update["message"]
        chat_id = str(msg["chat"]["id"])
        chat_type = msg["chat"]["type"]
        text = msg.get("text", "").strip()
        
        if chat_id not in subscribed_users:
            with data_lock:
                subscribed_users.add(chat_id)
                save_all()

        if text == "/start":
            handle_start(chat_id)
            return

        if chat_type != "private":
            send_telegram_message("❌ শুধুমাত্র প্রাইভেট চ্যাটে কাজ করে।", chat_id)
            return

        # ===== মেনু বাটন =====
        if text == "📤 কুকি সাবমিট করুন":
            send_telegram_message(
                "📤 আপনার কুকি টেক্সট লিখে পাঠান।\nঅথবা `.txt` ফাইল আপলোড করুন।\nবাতিল করতে /cancel লিখুন।",
                chat_id
            )
            return

        if text == "📊 API স্ট্যাটাস":
            handle_status(chat_id)
            return

        if text == "⏳ পেন্ডিং দেখুন":
            handle_pending_list(chat_id)
            return

        if text == "⚙️ অ্যাডমিন প্যানেল" and chat_id == ADMIN_CHAT_ID:
            admin_stats(chat_id)
            return

        if text == "📊 সার্বিক পরিসংখ্যান" and chat_id == ADMIN_CHAT_ID:
            admin_stats(chat_id)
            return

        if text == "📤 ফোর্স পুশ" and chat_id == ADMIN_CHAT_ID:
            force_push(chat_id)
            return

        if text == "🗑️ পেন্ডিং ক্লিয়ার" and chat_id == ADMIN_CHAT_ID:
            clear_pending(chat_id)
            return

        if text == "🔧 মেইন্টেনেন্স টগল" and chat_id == ADMIN_CHAT_ID:
            toggle_maintenance(chat_id)
            return

        if text == "🔙 মূল মেনু":
            send_telegram_message("মূল মেনু", chat_id, reply_markup=get_main_keyboard(chat_id))
            return

        if text == "/cancel":
            send_telegram_message("❌ বাতিল করা হয়েছে।", chat_id, reply_markup=get_main_keyboard(chat_id))
            return

        # ===== ডকুমেন্ট (ফাইল) =====
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
                    if file_content.strip():
                        handle_cookie_submission(chat_id, file_content.strip())
                    else:
                        send_telegram_message("❌ ফাইলটি খালি।", chat_id)
                except Exception as e:
                    logger.error(f"ফাইল রিড error: {e}")
                    send_telegram_message("❌ ফাইল পড়তে সমস্যা হয়েছে।", chat_id)
                return

        # ===== সাধারণ টেক্সট (কুকি) =====
        if text and not text.startswith("/"):
            # মেনু আইটেম চেক
            menu_items = [
                "📤 কুকি সাবমিট করুন", "📊 API স্ট্যাটাস", "⏳ পেন্ডিং দেখুন",
                "⚙️ অ্যাডমিন প্যানেল", "📊 সার্বিক পরিসংখ্যান", "📤 ফোর্স পুশ",
                "🗑️ পেন্ডিং ক্লিয়ার", "🔧 মেইন্টেনেন্স টগল", "🔙 মূল মেনু"
            ]
            if text not in menu_items:
                handle_cookie_submission(chat_id, text)

    elif "callback_query" in update:
        cb = update["callback_query"]
        chat_id = str(cb["message"]["chat"]["id"])
        answer_callback(cb["id"])

# ================== ফ্লাস্ক ==================
@app.route("/")
def home():
    return "Cookie Pusher Bot is Running!"

# ================== মেইন ==================
if __name__ == "__main__":
    load_all()
    fetch_api_status()
    logger.info(f"প্রাথমিক API স্ট্যাটাস: {'অনলাইন' if api_status['online'] else 'অফলাইন'}")

    threading.Thread(target=api_monitor_loop, daemon=True).start()
    threading.Thread(target=handle_updates, daemon=True).start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)

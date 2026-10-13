# Colored by 𝙺𝙸𝙽𝙶 𝙼𝙾𝙳 (@K_I_G_M)
import json
import os
import re
import secrets
import random
import threading
import time
from datetime import datetime

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = 7966430609

DZD_PER_USD_FLEXY = 30000.0
DZD_PER_USD_CCP = 25000.0
EGP_PER_USD = 60.0
FEE_PERCENT = 20.0
REFERRAL_PERCENT = 5.0

FLEXY_PHONE = "05 - 06 - 07"
CCP_NUMBER = "0079999"

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
db_lock = threading.RLock()

states = {}
state_lock = threading.RLock()

DEFAULT_DB = {
    "settings": {
        "flexy_phone": FLEXY_PHONE,
        "ccp_number": CCP_NUMBER,
        "egypt_instapay_number": "",
        "egypt_vodafone_number": "",
        "withdrawals_open": False,
        "ccp_enabled": True,
        "egypt_enabled": True,
    },
    "users": {},
    "purchases": {},
    "withdrawals": {},
    "states": {},
    "counter": 1000
}


def _db_connect():
    if not DATABASE_URL:
        raise RuntimeError(
            "Missing DATABASE_URL. Add a Railway PostgreSQL database and expose its "
            "connection string as the DATABASE_URL service variable."
        )
    import psycopg
    return psycopg.connect(DATABASE_URL, connect_timeout=10)


def _ensure_db_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                id SMALLINT PRIMARY KEY CHECK (id = 1),
                data JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
    conn.commit()


def _normalize_db(data):
    data.setdefault("settings", {})
    data["settings"].setdefault("flexy_phone", FLEXY_PHONE)
    data["settings"].setdefault("ccp_number", CCP_NUMBER)
    data["settings"].setdefault("egypt_instapay_number", "")
    data["settings"].setdefault("egypt_vodafone_number", "")
    data["settings"].setdefault("withdrawals_open", False)
    data["settings"].setdefault("ccp_enabled", True)
    data["settings"].setdefault("egypt_enabled", True)
    data.setdefault("users", {})
    data.setdefault("purchases", {})
    data.setdefault("withdrawals", {})
    data.setdefault("states", {})
    data.setdefault("counter", 1000)
    return data


def load_db():
    # All persistent bot state is stored in Railway PostgreSQL, not the container filesystem.
    with _db_connect() as conn:
        _ensure_db_table(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM bot_state WHERE id = 1")
            row = cur.fetchone()
        if row:
            return _normalize_db(row[0])

        # One-time best-effort import from the old JSON file, if it exists.
        legacy_data = None
        if os.path.exists("data.json"):
            try:
                with open("data.json", "r", encoding="utf-8") as f:
                    legacy_data = json.load(f)
            except (OSError, json.JSONDecodeError):
                legacy_data = None

        data = _normalize_db(legacy_data if isinstance(legacy_data, dict)
                             else json.loads(json.dumps(DEFAULT_DB)))
        save_db(data)
        return data


def save_db(data):
    from psycopg.types.json import Jsonb
    # Use one JSONB row so all existing bot features keep their current data shape,
    # while PostgreSQL provides durable storage across redeploys/restarts.
    with db_lock:
        with _db_connect() as conn:
            _ensure_db_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bot_state (id, data, updated_at)
                    VALUES (1, %s, NOW())
                    ON CONFLICT (id)
                    DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()
                    """,
                    (Jsonb(data),),
                )
            conn.commit()


db = load_db()
states = db.get("states", {})


CAPTCHA_SYMBOLS = ["🐉", "⭐", "🍉", "🍎"]
CAPTCHA_TIMEOUT_SECONDS = 120
CAPTCHA_MAX_ATTEMPTS = 3
CAPTCHA_LOCK_SECONDS = 60


def is_user_verified(user_id):
    user = db.get("users", {}).get(str(user_id))
    # Existing users from before this feature remain trusted; only newly registered
    # users explicitly marked False must complete the captcha.
    return bool(user is not None and user.get("captcha_verified", True))


def send_captcha(chat_id, user_id):
    uid = str(user_id)
    with db_lock:
        user = db["users"].get(uid)
        if not user:
            return
        blocked_until = float(user.get("captcha_blocked_until", 0) or 0)
        remaining = int(blocked_until - time.time())
        if remaining > 0:
            safe_send(chat_id, f"🛡️ حاولت مرات كثيرة. عاود بعد {remaining} ثانية.")
            return
        symbols = list(CAPTCHA_SYMBOLS)
        random.shuffle(symbols)
        target = random.choice(symbols)
        nonce = secrets.token_hex(4)
        user["captcha_nonce"] = nonce
        user["captcha_answer"] = symbols.index(target)
        user["captcha_expires"] = time.time() + CAPTCHA_TIMEOUT_SECONDS
        save_db(db)

    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        types.InlineKeyboardButton(symbol, callback_data=f"captcha:{nonce}:{index}")
        for index, symbol in enumerate(symbols)
    ]
    kb.add(*buttons)
    safe_send(
        chat_id,
        "🛡️ <b>تحقّق أمني سريع</b>\n\n"
        f"اضغط على الرمز التالي: <b>{target}</b>\n"
        "عندك دقيقتين لإكمال التحقق حتى تقدر تستعمل البوت.",
        reply_markup=kb,
    )


def require_verified_callback(call):
    if is_user_verified(call.from_user.id):
        return True
    bot.answer_callback_query(call.id, "🛡️ أكمل الكابتشا أولاً.")
    return False


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def money(x):
    return f"{float(x):.2f}"


def normalize_amount(text):
    s = str(text).strip().lower()
    s = s.replace("دج", "").replace("da", "").replace("dz", "")
    s = s.replace(" ", "").replace("_", "")
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):
        s = re.sub(r"[.,]", "", s)
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    return None


def normalize_usd(text):
    s = str(text).strip().replace("$", "").replace("دولار", "")
    s = s.replace(",", ".").replace(" ", "")
    try:
        value = float(s)
        if value <= 0:
            return None
        return round(value, 8)
    except Exception:
        return None


def get_user(user_obj):
    uid = str(user_obj.id)
    with db_lock:
        if uid not in db["users"]:
            db["users"][uid] = {
                "id": user_obj.id,
                "username": user_obj.username or "",
                "first_name": user_obj.first_name or "",
                "last_name": user_obj.last_name or "",
                "balance": 0.0,
                "joined_at": now_str(),
                "successful_purchases": 0,
                "successful_withdrawals": 0,
                "purchase_count": 0,
                "withdrawal_count": 0,
                "banned": False,
                "captcha_verified": False,
                "captcha_attempts": 0,
                "referred_by": None,
                "referral_earnings": 0.0,
            }
            save_db(db)
        else:
            u = db["users"][uid]
            u["username"] = user_obj.username or u.get("username", "")
            u["first_name"] = user_obj.first_name or u.get("first_name", "")
            u["last_name"] = user_obj.last_name or u.get("last_name", "")
            u.setdefault("referred_by", None)
            u.setdefault("referral_earnings", 0.0)
            save_db(db)
        return db["users"][uid]


def get_user_by_id(uid):
    return db["users"].get(str(uid))


def set_state(uid, state, data=None):
    key = str(uid)
    # Persist conversation/admin workflow state so restarts do not lose progress.
    with db_lock:
        with state_lock:
            states[key] = {"state": state, "data": data or {}}
            db["states"] = states
        save_db(db)


def get_state(uid):
    with state_lock:
        return states.get(str(uid))


def clear_state(uid):
    key = str(uid)
    with db_lock:
        with state_lock:
            states.pop(key, None)
            db["states"] = states
        save_db(db)


def next_operation_id():
    with db_lock:
        db["counter"] += 1
        save_db(db)
        return db["counter"]


def is_admin(user_id):
    return int(user_id) == int(ADMIN_ID)


def is_banned(user_id):
    with db_lock:
        u = db["users"].get(str(user_id))
        return bool(u and u.get("banned", False))


def safe_send(chat_id, text, **kwargs):
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        print("send_message error:", chat_id, e)
        return None


def user_display(u):
    if not u:
        return "غير معروف"
    name = (u.get("first_name", "") + " " + u.get("last_name", "")).strip()
    username = f"@{u['username']}" if u.get("username") else "بدون يوزرنيم"
    return f"{name or 'بدون اسم'} | {username} | ID: <code>{u['id']}</code>"


def delete_old_keyboard(chat_id, user_id):
    try:
        msg = safe_send(chat_id, ".", reply_markup=types.ReplyKeyboardRemove())
        if msg:
            bot.delete_message(chat_id, msg.message_id)
    except Exception:
        pass


def main_inline_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("شراء دولار", callback_data="menu_buy", style="success", icon_custom_emoji_id="5452008674971122988"),
        types.InlineKeyboardButton("سحب الدولار", callback_data="menu_withdraw", style="success", icon_custom_emoji_id="4956574641075258382"),
    )
    kb.add(
        types.InlineKeyboardButton("رصيدي", callback_data="menu_balance", style="danger", icon_custom_emoji_id="5386741870904768459"),
        types.InlineKeyboardButton("معلوماتي", callback_data="menu_info", style="danger", icon_custom_emoji_id="5462909460882144593"),
    )
    kb.add(
        types.InlineKeyboardButton("رابط الإحالة", callback_data="menu_referral", style="primary", icon_custom_emoji_id="5463213484437164361"),
        types.InlineKeyboardButton("ℹ معلومات الشراء", callback_data="menu_howto", style="primary", icon_custom_emoji_id="5462985825400671027"),
    )
    kb.add(
        types.InlineKeyboardButton("مالك البوت", callback_data="menu_owner", style="success"),
    )
    return kb


def admin_inline_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("إذاعة", callback_data="adm_broadcast", style="success"),
        types.InlineKeyboardButton("رقم الفليكسي", callback_data="adm_flexy_phone", style="success"),
    )
    kb.add(
        types.InlineKeyboardButton("رقم CCP", callback_data="adm_ccp_number", style="danger"),
        types.InlineKeyboardButton("أرقام مصر", callback_data="adm_egypt_numbers", style="danger"),
    )
    kb.add(
        types.InlineKeyboardButton("السحب: تشغيل/إيقاف", callback_data="adm_toggle_withdraw", style="primary"),
        types.InlineKeyboardButton("فحص عضو", callback_data="adm_check_member", style="primary"),
    )
    kb.add(
        types.InlineKeyboardButton("CCP: تشغيل/إيقاف", callback_data="adm_toggle_ccp", style="success"),
        types.InlineKeyboardButton("مصر: تشغيل/إيقاف", callback_data="adm_toggle_egypt", style="success"),
    )
    kb.add(
        types.InlineKeyboardButton("الرئيسية", callback_data="adm_home", style="danger"),
    )
    return kb


def back_inline(callback="back_main"):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("رجوع", callback_data=callback, style="success"))
    return kb


def confirm_inline(confirm_cb, cancel_cb="back_main"):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("مواصلة", callback_data=confirm_cb, style="success"),
        types.InlineKeyboardButton("رجوع", callback_data=cancel_cb, style="success"),
    )
    return kb


def configure_commands():
    try:
        user_commands = [types.BotCommand("start", "فتح القائمة الرئيسية")]
        admin_commands = [
            types.BotCommand("start", "فتح القائمة الرئيسية"),
            types.BotCommand("banantak", "فتح لوحة الأدمن"),
        ]
        bot.set_my_commands(user_commands)
        scope = types.BotCommandScopeChat(chat_id=ADMIN_ID)
        bot.set_my_commands(admin_commands, scope=scope)
        print("Bot commands configured.")
    except Exception as e:
        print("Could not configure commands:", e)


@bot.message_handler(commands=["start"])
def start_command(message):
    uid = message.from_user.id
    args = message.text.split()

    u = get_user(message.from_user)

    if u.get("banned"):
        safe_send(message.chat.id, "🚫 حسابك محظور من استخدام البوت.")
        return

    if len(args) > 1:
        ref_code = args[1]
        if ref_code.startswith("ref_"):
            referrer_id = ref_code[4:]
            if (
                str(referrer_id) != str(uid)
                and not u.get("referred_by")
                and db["users"].get(str(referrer_id))
            ):
                with db_lock:
                    db["users"][str(uid)]["referred_by"] = referrer_id
                    save_db(db)

    if not is_user_verified(uid):
        send_captcha(message.chat.id, uid)
        return

    clear_state(uid)

    try:
        remove_msg = bot.send_message(message.chat.id, ".", reply_markup=types.ReplyKeyboardRemove())
        bot.delete_message(message.chat.id, remove_msg.message_id)
    except Exception:
        pass

    text = (
        "✨ <b>أهلاً بك في بوت الدولار</b> ✨\n\n"
        "💵 شراء الدولار بسهولة\n"
        "💸 سحب رصيدك عند فتح السحب\n"
        "💰 متابعة رصيدك وعملياتك\n"
        "🔗 دعوة أصدقاء وكسب 5% من كل شحن\n\n"
        "👇 <b>اختر الخدمة التي تحتاجها</b>"
    )
    safe_send(message.chat.id, text, reply_markup=main_inline_keyboard())


@bot.message_handler(commands=["banantak"])
def admin_command(message):
    if not is_user_verified(message.from_user.id):
        send_captcha(message.chat.id, message.from_user.id)
        return
    if not is_admin(message.from_user.id):
        safe_send(message.chat.id, "❌ هذا الأمر خاص بالأدمن.")
        return

    clear_state(message.from_user.id)

    try:
        remove_msg = bot.send_message(message.chat.id, ".", reply_markup=types.ReplyKeyboardRemove())
        bot.delete_message(message.chat.id, remove_msg.message_id)
    except Exception:
        pass

    send_admin_panel(message.chat.id)


def send_admin_panel(chat_id, message_id=None):
    with db_lock:
        users_count = len(db["users"])
        purchases_pending = sum(1 for p in db["purchases"].values() if p.get("status") == "pending")
        withdrawals_pending = sum(1 for w in db["withdrawals"].values() if w.get("status") == "pending")
        opened = db["settings"].get("withdrawals_open", False)
        ccp_enabled = db["settings"].get("ccp_enabled", True)
        egypt_enabled = db["settings"].get("egypt_enabled", True)
        phone = db["settings"].get("flexy_phone", FLEXY_PHONE)
        ccp = db["settings"].get("ccp_number", CCP_NUMBER)

    status_w = "🟢 مفتوح" if opened else "🔴 مغلق"
    status_ccp = "🟢 مفعل" if ccp_enabled else "🔴 معطل"
    status_egy = "🟢 مفعل" if egypt_enabled else "🔴 معطل"

    text = (
        "🛠 <b>لوحة الأدمن</b>\n\n"
        f"👥 الأعضاء: <b>{users_count}</b>\n"
        f"🧾 شراء معلق: <b>{purchases_pending}</b>\n"
        f"💸 سحب معلق: <b>{withdrawals_pending}</b>\n"
        f"📱 رقم الفليكسي: <code>{phone}</code>\n"
        f"🏦 رقم CCP: <code>{ccp}</code>\n"
        f"💰 السحب: <b>{status_w}</b>\n"
        f"🏦 CCP: <b>{status_ccp}</b>\n"
        f"🇪🇬 مصر: <b>{status_egy}</b>\n"
        f"💵 سعر الفليكسي: <b>{DZD_PER_USD_FLEXY:,.0f} دج/$</b>\n"
        f"🏦 سعر CCP: <b>{DZD_PER_USD_CCP:,.0f} دج/$</b>\n"
        f"🇪🇬 سعر مصر: <b>{EGP_PER_USD:,.0f} جنيه/$</b>\n"
        f"📊 العمولة: <b>{FEE_PERCENT:.0f}%</b>"
    )

    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=admin_inline_keyboard())
            return
        except Exception:
            pass

    safe_send(chat_id, text, reply_markup=admin_inline_keyboard())


def send_main_menu(chat_id, message_id=None, edit=False):
    text = (
        "🏠 <b>القائمة الرئيسية</b>\n\n"
        "👇 اختر الخدمة التي تحتاجها"
    )
    if edit and message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=main_inline_keyboard())
            return
        except Exception:
            pass
    safe_send(chat_id, text, reply_markup=main_inline_keyboard())


@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def cb_back_main(call):
    if not require_verified_callback(call):
        return
    uid = call.from_user.id
    clear_state(uid)
    if is_admin(uid):
        send_admin_panel(call.message.chat.id, call.message.message_id)
    else:
        send_main_menu(call.message.chat.id, call.message.message_id, edit=True)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "owner_back")
def cb_owner_back(call):
    if not require_verified_callback(call):
        return
    clear_state(call.from_user.id)
    send_main_menu(call.message.chat.id, call.message.message_id, edit=True)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "adm_home")
def cb_adm_home(call):
    if not require_verified_callback(call):
        return
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌")
        return
    clear_state(call.from_user.id)
    send_admin_panel(call.message.chat.id, call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("menu_"))
def main_menu_callback(call):
    if not require_verified_callback(call):
        return
    uid = call.from_user.id
    u = get_user(call.from_user)
    bot.answer_callback_query(call.id)

    if u.get("banned"):
        bot.edit_message_text("🚫 حسابك محظور.", call.message.chat.id, call.message.message_id)
        return

    action = call.data

    if action == "menu_buy":
        show_country_selection(call.message.chat.id, call.message.message_id)

    elif action == "menu_withdraw":
        handle_withdraw_start(call.message, uid)

    elif action == "menu_balance":
        balance = float(u.get("balance", 0))
        try:
            bot.edit_message_text(
                f"💰 <b>رصيدك الحالي:</b> {money(balance)}$",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=back_inline()
            )
        except Exception:
            pass

    elif action == "menu_info":
        username = f"@{u['username']}" if u.get("username") else "بدون يوزرنيم"
        name = (u.get("first_name", "") + " " + u.get("last_name", "")).strip() or "بدون اسم"
        text = (
            "👤 <b>معلوماتي</b>\n\n"
            f"📝 الاسم: <b>{name}</b>\n"
            f"🔹 اليوزرنيم: <b>{username}</b>\n"
            f"🆔 الـID: <code>{u['id']}</code>\n"
            f"💰 الرصيد: <b>{money(u.get('balance', 0))}$</b>\n"
            f"🔗 عائد الإحالة: <b>{money(u.get('referral_earnings', 0))}$</b>"
        )
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=back_inline())
        except Exception:
            pass

    elif action == "menu_referral":
        bot_info = bot.get_me()
        link = f"https://t.me/{bot_info.username}?start=ref_{uid}"
        text = (
            "🔗 <b>رابط الإحالة الخاص بك</b>\n\n"
            f"<code>{link}</code>\n\n"
            f"شارك هذا الرابط وستكسب <b>{REFERRAL_PERCENT:.0f}%</b> من كل عملية شحن يقوم بها من يدخل عبر رابطك.\n\n"
            f"💰 إجمالي ما كسبته من الإحالات: <b>{money(u.get('referral_earnings', 0))}$</b>"
        )
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=back_inline())
        except Exception:
            pass

    elif action == "menu_howto":
        phone = db["settings"].get("flexy_phone", FLEXY_PHONE)
        ccp = db["settings"].get("ccp_number", CCP_NUMBER)
        text = (
            "ℹ️ <b>طريقة شراء الدولار</b>\n\n"
            "🇩🇿 <b>الجزائر - فليكسي:</b>\n"
            f"• سعر: <b>1$ = {DZD_PER_USD_FLEXY:,.0f} دج</b>\n"
            f"• عمولة: <b>{FEE_PERCENT:.0f}%</b>\n"
            f"• الرقم: <code>{phone}</code>\n\n"
            "🇩🇿 <b>الجزائر - بريدي موب (CCP):</b>\n"
            f"• سعر: <b>1$ = {DZD_PER_USD_CCP:,.0f} دج</b>\n"
            f"• عمولة: <b>{FEE_PERCENT:.0f}%</b>\n"
            f"• رقم الحساب: <code>{ccp}</code>\n\n"
            "🇪🇬 <b>مصر:</b>\n"
            f"• سعر: <b>1$ = {EGP_PER_USD:,.0f} جنيه</b>\n"
            f"• عمولة: <b>{FEE_PERCENT:.0f}%</b>\n"
            "• طرق الدفع: إنستاباي / فودافون كاش\n\n"
            "⚠️ الحد الأدنى للشحن: <b>1$</b>"
        )
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=back_inline())
        except Exception:
            pass

    elif action == "menu_owner":
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("فتح بروفايل مالك البوت", url="https://t.me/hwa_y55", style="success"))
        kb.add(types.InlineKeyboardButton("رجوع", callback_data="owner_back", style="danger"))
        try:
            bot.edit_message_text(
                "👑 <b>مالك البوت</b>\n\n"
                "للتواصل مع مالك البوت أو طلب المساعدة:\n"
                "🔗 <b>@hwa_y55</b>",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=kb
            )
        except Exception:
            pass


def show_country_selection(chat_id, message_id=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("الجزائر", callback_data="country_dz", style="success"))
    kb.add(types.InlineKeyboardButton("مصر", callback_data="country_eg", style="danger"))
    kb.add(types.InlineKeyboardButton("باقي دول العالم", callback_data="country_other", style="primary"))
    kb.add(types.InlineKeyboardButton("رجوع", callback_data="back_main", style="success"))

    text = "💵 <b>شراء دولار</b>\n\nاختر بلدك:"

    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
            return
        except Exception:
            pass
    safe_send(chat_id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("country_"))
def country_callback(call):
    if not require_verified_callback(call):
        return
    uid = call.from_user.id
    bot.answer_callback_query(call.id)

    country = call.data.split("_", 1)[1]

    if country == "dz":
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("فليكسي", callback_data="pay_dz_flexy", style="success"))
        with db_lock:
            ccp_on = db["settings"].get("ccp_enabled", True)
        if ccp_on:
            kb.add(types.InlineKeyboardButton("بريدي موب (CCP)", callback_data="pay_dz_ccp", style="danger"))
        kb.add(types.InlineKeyboardButton("رجوع", callback_data="back_country", style="primary"))
        try:
            bot.edit_message_text(
                "🇩🇿 <b>الجزائر</b>\n\nاختر طريقة الدفع:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=kb
            )
        except Exception:
            pass

    elif country == "eg":
        with db_lock:
            egypt_on = db["settings"].get("egypt_enabled", True)
        if not egypt_on:
            try:
                bot.edit_message_text(
                    "❌ خدمة مصر غير متاحة حالياً.",
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=back_inline("back_country")
                )
            except Exception:
                pass
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("إنستاباي", callback_data="pay_eg_instapay", style="success"))
        kb.add(types.InlineKeyboardButton("فودافون كاش", callback_data="pay_eg_vodafone", style="danger"))
        kb.add(types.InlineKeyboardButton("رجوع", callback_data="back_country", style="primary"))
        try:
            bot.edit_message_text(
                "🇪🇬 <b>مصر</b>\n\nاختر طريقة الدفع:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=kb
            )
        except Exception:
            pass

    elif country == "other":
        set_state(uid, "other_country_name")
        try:
            bot.edit_message_text(
                "🌍 <b>باقي دول العالم</b>\n\n"
                "سنطرح عليك بعض الأسئلة لإكمال طلبك.\n\n"
                "السؤال 1️⃣: ما هو <b>اسم بلدك</b>؟\n\n"
                "أرسل اسم بلدك الآن:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=back_inline("back_country")
            )
        except Exception:
            pass


@bot.callback_query_handler(func=lambda call: call.data == "back_country")
def cb_back_country(call):
    if not require_verified_callback(call):
        return
    bot.answer_callback_query(call.id)
    clear_state(call.from_user.id)
    show_country_selection(call.message.chat.id, call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_dz_"))
def pay_dz_callback(call):
    if not require_verified_callback(call):
        return
    uid = call.from_user.id
    bot.answer_callback_query(call.id)
    method = call.data.split("pay_dz_")[1]

    if method == "flexy":
        set_state(uid, "purchase_amount", {"method": "flexy"})
        phone = db["settings"].get("flexy_phone", FLEXY_PHONE)
        try:
            bot.edit_message_text(
                f"📱 <b>شحن بالفليكسي</b>\n\n"
                f"💵 السعر: <b>1$ = {DZD_PER_USD_FLEXY:,.0f} دج</b>\n"
                f"📊 العمولة: <b>{FEE_PERCENT:.0f}%</b>\n\n"
                "اكتب مبلغ الفليكسي بالدينار الذي تريد إرساله.\n"
                "مثال: <code>100000</code> أو <code>100.000</code>",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=back_inline("back_country")
            )
        except Exception:
            pass

    elif method == "ccp":
        with db_lock:
            ccp_on = db["settings"].get("ccp_enabled", True)
        if not ccp_on:
            try:
                bot.edit_message_text(
                    "❌ خدمة بريدي موب معطلة حالياً.",
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=back_inline("back_country")
                )
            except Exception:
                pass
            return
        set_state(uid, "purchase_amount", {"method": "ccp"})
        ccp = db["settings"].get("ccp_number", CCP_NUMBER)
        try:
            bot.edit_message_text(
                f"🏦 <b>شحن ببريدي موب (CCP)</b>\n\n"
                f"💵 السعر: <b>1$ = {DZD_PER_USD_CCP:,.0f} دج</b>\n"
                f"📊 العمولة: <b>{FEE_PERCENT:.0f}%</b>\n"
                f"🏦 رقم الحساب: <code>{ccp}</code>\n\n"
                "اكتب المبلغ الذي أرسلته بالدينار.\n"
                "مثال: <code>50000</code>",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=back_inline("back_country")
            )
        except Exception:
            pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_eg_"))
def pay_eg_callback(call):
    if not require_verified_callback(call):
        return
    uid = call.from_user.id
    bot.answer_callback_query(call.id)
    method = call.data.split("pay_eg_")[1]

    with db_lock:
        egypt_on = db["settings"].get("egypt_enabled", True)
        if method == "instapay":
            eg_number = db["settings"].get("egypt_instapay_number", "")
        else:
            eg_number = db["settings"].get("egypt_vodafone_number", "")

    if not egypt_on:
        try:
            bot.edit_message_text(
                "❌ خدمة مصر غير متاحة حالياً.",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=back_inline("back_country")
            )
        except Exception:
            pass
        return

    if not eg_number:
        try:
            bot.edit_message_text(
                "❌ لم يتم ضبط رقم هذه الطريقة بعد. تواصل مع الدعم: <b>@hwa_y55</b>",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=back_inline("back_country")
            )
        except Exception:
            pass
        return

    method_name = "إنستاباي" if method == "instapay" else "فودافون كاش"
    set_state(uid, "purchase_amount", {"method": f"egypt_{method}", "eg_number": eg_number})

    try:
        bot.edit_message_text(
            f"🇪🇬 <b>شحن عبر {method_name}</b>\n\n"
            f"💵 السعر: <b>1$ = {EGP_PER_USD:,.0f} جنيه</b>\n"
            f"📊 العمولة: <b>{FEE_PERCENT:.0f}%</b>\n"
            f"📱 الرقم: <code>{eg_number}</code>\n\n"
            "اكتب المبلغ الذي أرسلته بالجنيه المصري.\n"
            "مثال: <code>6000</code>",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=back_inline("back_country")
        )
    except Exception:
        pass


@bot.message_handler(content_types=["text"])
def text_handler(message):
    uid = message.from_user.id
    text = (message.text or "").strip()

    u = get_user(message.from_user)

    if u.get("banned"):
        safe_send(message.chat.id, "🚫 حسابك محظور من استخدام البوت.")
        return

    if not is_user_verified(uid):
        send_captcha(message.chat.id, uid)
        return

    if text.startswith("/"):
        return

    st = get_state(uid)

    if is_admin(uid) and st:
        state = st["state"]

        if state == "admin_change_phone":
            process_change_phone_text(message)
            return

        if state == "admin_change_ccp":
            process_change_ccp_text(message)
            return

        if state == "admin_egypt_instapay":
            process_egypt_instapay_text(message)
            return

        if state == "admin_egypt_vodafone":
            process_egypt_vodafone_text(message)
            return

        if state == "admin_check_member":
            process_check_member_text(message)
            return

        if state == "admin_member_add":
            admin_member_change(message, add=True)
            return

        if state == "admin_member_sub":
            admin_member_change(message, add=False)
            return

        if state == "admin_broadcast":
            broadcast_copy(message)
            return

    if st:
        state = st["state"]

        if state == "purchase_amount":
            process_purchase_amount(message, st["data"])
            return

        if state == "purchase_confirm":
            safe_send(message.chat.id, "اضغط أحد الأزرار في الرسالة أعلاه ✅")
            return

        if state == "purchase_proof":
            safe_send(message.chat.id, "📸 أرسل <b>صورة</b> وصل الدفع مع كتابة الوقت في وصف الصورة.")
            return

        if state == "withdraw_amount":
            process_withdraw_amount_text(message)
            return

        if state == "withdraw_binance":
            process_withdraw_binance_text(message)
            return

        if state == "other_country_name":
            process_other_country_name(message)
            return

        if state == "other_currency_code":
            process_other_currency_code(message)
            return

        if state == "other_payment_method":
            process_other_payment_method(message)
            return

        if state == "other_payment_details":
            process_other_payment_details(message)
            return

        if state == "other_usd_amount":
            process_other_usd_amount(message)
            return

    safe_send(message.chat.id, "👇 اختر من القائمة:", reply_markup=main_inline_keyboard())


def process_purchase_amount(message, state_data):
    uid = message.from_user.id
    method = state_data.get("method", "flexy")

    is_egypt = method.startswith("egypt_")

    if is_egypt:
        raw = normalize_amount(message.text)
        if raw is None or raw <= 0:
            safe_send(message.chat.id, "❌ المبلغ غير صحيح. أرسل مثلاً: <code>6000</code>")
            return
        rate = EGP_PER_USD
        fee = raw * (FEE_PERCENT / 100.0)
        net = raw - fee
        usd = round(net / rate, 8)
        currency_label = "جنيه مصري"
        method_name = "إنستاباي" if "instapay" in method else "فودافون كاش"
        eg_number = state_data.get("eg_number", "")

        if usd < 1.0:
            safe_send(message.chat.id, f"❌ الحد الأدنى للشحن هو 1$. أرسل مبلغاً أكبر.")
            return

        set_state(uid, "purchase_confirm", {
            "method": method,
            "raw_amount": raw,
            "fee": fee,
            "net": net,
            "usd": usd,
            "eg_number": eg_number,
        })

        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("مواصلة", callback_data="confirm_purchase", style="success"),
            types.InlineKeyboardButton("رجوع", callback_data="back_country", style="success"),
        )

        text = (
            f"🧾 <b>مراجعة العملية</b>\n\n"
            f"🇪🇬 الطريقة: <b>{method_name}</b>\n"
            f"💵 المبلغ: <b>{raw:,.0f} {currency_label}</b>\n"
            f"📊 العمولة ({FEE_PERCENT:.0f}%): <b>{fee:,.0f} {currency_label}</b>\n"
            f"💰 المحتسب: <b>{net:,.0f} {currency_label}</b>\n"
            f"💲 سعر الدولار: <b>{rate:,.0f} {currency_label}</b>\n"
            f"💵 سيضاف لك: <b>{money(usd)}$</b>\n\n"
            f"📱 الرقم: <code>{eg_number}</code>\n\n"
            "هل تريد المواصلة؟"
        )
        safe_send(message.chat.id, text, reply_markup=kb)

    else:
        raw = normalize_amount(message.text)
        if raw is None or raw <= 0:
            safe_send(message.chat.id, "❌ المبلغ غير صحيح. أرسل مثلاً: <code>100000</code>")
            return

        rate = DZD_PER_USD_CCP if method == "ccp" else DZD_PER_USD_FLEXY
        fee = raw * (FEE_PERCENT / 100.0)
        net = raw - fee
        usd = round(net / rate, 8)

        if usd < 1.0:
            safe_send(message.chat.id, f"❌ الحد الأدنى للشحن هو 1$. أرسل مبلغاً أكبر.")
            return

        set_state(uid, "purchase_confirm", {
            "method": method,
            "raw_amount": raw,
            "fee": fee,
            "net": net,
            "usd": usd,
        })

        method_name = "فليكسي" if method == "flexy" else "بريدي موب (CCP)"

        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("مواصلة", callback_data="confirm_purchase", style="success"),
            types.InlineKeyboardButton("رجوع", callback_data="back_country", style="success"),
        )

        text = (
            f"🧾 <b>مراجعة العملية</b>\n\n"
            f"🇩🇿 الطريقة: <b>{method_name}</b>\n"
            f"💵 المبلغ: <b>{raw:,.0f} دج</b>\n"
            f"📊 العمولة ({FEE_PERCENT:.0f}%): <b>{fee:,.0f} دج</b>\n"
            f"💰 المحتسب: <b>{net:,.0f} دج</b>\n"
            f"💲 سعر الدولار: <b>{rate:,.0f} دج</b>\n"
            f"💵 سيضاف لك: <b>{money(usd)}$</b>\n\n"
            "هل تريد المواصلة؟"
        )
        safe_send(message.chat.id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data == "confirm_purchase")
def cb_confirm_purchase(call):
    if not require_verified_callback(call):
        return
    uid = call.from_user.id
    bot.answer_callback_query(call.id)
    st = get_state(uid)

    if not st or st["state"] != "purchase_confirm":
        send_main_menu(call.message.chat.id, call.message.message_id, edit=True)
        return

    data = st["data"]
    method = data.get("method", "flexy")

    set_state(uid, "purchase_proof", data)

    if method == "flexy":
        phone = db["settings"].get("flexy_phone", FLEXY_PHONE)
        text = (
            "📱 <b>طريقة الدفع - فليكسي</b>\n\n"
            f"اذهب إلى محل الفليكسي وأرسل المبلغ إلى:\n"
            f"<code>{phone}</code>\n\n"
            f"💵 المبلغ: <b>{data['raw_amount']:,.0f} دج</b>\n"
            f"💰 المقابل: <b>{money(data['usd'])}$</b>\n\n"
            "بعد الإرسال:\n"
            "1️⃣ صوّر وصل الفليكسي.\n"
            "2️⃣ أرسل الصورة هنا مع كتابة الوقت (مثال: <code>14:35</code>) في وصف الصورة.\n\n"
            "⚠️ أرسل فليكسي عادي وليس ACTIF/ACTIVÉ."
        )
    elif method == "ccp":
        ccp = db["settings"].get("ccp_number", CCP_NUMBER)
        text = (
            "🏦 <b>طريقة الدفع - بريدي موب (CCP)</b>\n\n"
            f"حول المبلغ إلى رقم الحساب:\n"
            f"<code>{ccp}</code>\n\n"
            f"💵 المبلغ: <b>{data['raw_amount']:,.0f} دج</b>\n"
            f"💰 المقابل: <b>{money(data['usd'])}$</b>\n\n"
            "بعد التحويل:\n"
            "1️⃣ صوّر إثبات التحويل.\n"
            "2️⃣ أرسل الصورة هنا مع كتابة الوقت في وصف الصورة."
        )
    elif method.startswith("egypt_"):
        eg_number = data.get("eg_number", "")
        method_name = "إنستاباي" if "instapay" in method else "فودافون كاش"
        text = (
            f"🇪🇬 <b>طريقة الدفع - {method_name}</b>\n\n"
            f"حول المبلغ إلى:\n"
            f"<code>{eg_number}</code>\n\n"
            f"💵 المبلغ: <b>{data['raw_amount']:,.0f} جنيه</b>\n"
            f"💰 المقابل: <b>{money(data['usd'])}$</b>\n\n"
            "بعد التحويل:\n"
            "1️⃣ صوّر إثبات التحويل.\n"
            "2️⃣ أرسل الصورة هنا مع كتابة الوقت في وصف الصورة."
        )
    else:
        text = "أرسل صورة إثبات الدفع مع الوقت في الوصف."

    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id)
    except Exception:
        safe_send(call.message.chat.id, text)


@bot.message_handler(content_types=["photo"])
def photo_handler(message):
    uid = message.from_user.id
    if not is_user_verified(uid):
        send_captcha(message.chat.id, uid)
        return
    st = get_state(uid)

    if not st or st["state"] != "purchase_proof":
        if is_admin(uid):
            admin_st = get_state(uid)
            if admin_st and admin_st["state"] == "admin_broadcast":
                broadcast_copy(message)
                return
        safe_send(message.chat.id, "❌ أولاً اضغط «شراء دولار» من القائمة.")
        return

    caption = message.caption or ""
    operation_id = next_operation_id()
    purchase_id = f"#{operation_id}"

    data = st["data"]
    method = data.get("method", "flexy")

    purchase = {
        "id": purchase_id,
        "user_id": uid,
        "method": method,
        "raw_amount": data.get("raw_amount", 0),
        "fee": data.get("fee", 0),
        "net": data.get("net", 0),
        "usd": data.get("usd", 0),
        "photo_file_id": message.photo[-1].file_id,
        "caption": caption,
        "time": extract_time(caption),
        "created_at": now_str(),
        "status": "pending",
    }

    with db_lock:
        db["purchases"][purchase_id] = purchase
        db["users"][str(uid)]["purchase_count"] = db["users"][str(uid)].get("purchase_count", 0) + 1
        save_db(db)

    clear_state(uid)

    safe_send(
        uid,
        f"📨 تم إرسال إثبات العملية <b>{purchase_id}</b> إلى الأدمن للمراجعة.\n"
        "⏳ انتظر نتيجة المراجعة.",
        reply_markup=main_inline_keyboard()
    )

    send_purchase_to_admin(purchase)


def extract_time(caption):
    if not caption:
        return None
    m = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", caption)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def send_purchase_to_admin(purchase):
    uid = purchase["user_id"]
    u = get_user_by_id(uid)
    method = purchase.get("method", "flexy")

    method_labels = {
        "flexy": "📱 فليكسي",
        "ccp": "🏦 بريدي موب",
        "egypt_instapay": "🇪🇬 إنستاباي",
        "egypt_vodafone": "🇪🇬 فودافون كاش",
    }
    method_label = method_labels.get(method, method)

    is_egypt = method.startswith("egypt_")
    currency = "جنيه" if is_egypt else "دج"

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("قبول", callback_data=f"purchase_accept:{purchase['id']}", style="success"),
        types.InlineKeyboardButton("رفض", callback_data=f"purchase_reject:{purchase['id']}", style="success"),
    )

    text = (
        "🧾 <b>عملية شحن جديدة</b>\n\n"
        f"🔢 العملية: <b>{purchase['id']}</b>\n"
        f"💳 الطريقة: <b>{method_label}</b>\n"
        f"👤 العضو: {user_display(u)}\n"
        f"💵 المبلغ: <b>{purchase['raw_amount']:,.0f} {currency}</b>\n"
        f"📊 العمولة: <b>{purchase['fee']:,.0f} {currency}</b>\n"
        f"💲 سيضاف: <b>{money(purchase['usd'])}$</b>\n"
        f"🕒 وقت الإرسال: <b>{purchase['created_at']}</b>\n"
        f"⏰ وقت العملية من العضو: <b>{purchase.get('time') or 'لم يذكر'}</b>\n"
        f"📝 الوصف: <code>{purchase.get('caption') or 'بدون وصف'}</code>"
    )

    try:
        bot.send_photo(ADMIN_ID, purchase["photo_file_id"], caption=text, reply_markup=kb)
    except Exception as e:
        print("send_purchase_to_admin error:", e)


@bot.callback_query_handler(func=lambda call: call.data.startswith("purchase_"))
def purchase_callback(call):
    if not require_verified_callback(call):
        return
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ غير مسموح.")
        return

    parts = call.data.split(":", 1)
    action = parts[0]
    purchase_id = parts[1] if len(parts) > 1 else ""

    with db_lock:
        purchase = db["purchases"].get(purchase_id)
        if not purchase:
            bot.answer_callback_query(call.id, "العملية غير موجودة.")
            return
        if purchase.get("status") != "pending":
            bot.answer_callback_query(call.id, "تمت معالجة هذه العملية سابقاً.")
            return

        uid = str(purchase["user_id"])

        if action == "purchase_accept":
            purchase["status"] = "accepted"
            purchase["approved_at"] = now_str()
            u = db["users"].get(uid)

            if u is None:
                bot.answer_callback_query(call.id, "العضو غير موجود.")
                return

            usd_amount = float(purchase["usd"])
            u["balance"] = round(float(u.get("balance", 0)) + usd_amount, 8)
            u["successful_purchases"] = u.get("successful_purchases", 0) + 1

            referral_bonus = 0.0
            referrer_id = u.get("referred_by")
            if referrer_id and db["users"].get(str(referrer_id)):
                referral_bonus = round(usd_amount * (REFERRAL_PERCENT / 100.0), 8)
                referrer = db["users"][str(referrer_id)]
                referrer["balance"] = round(float(referrer.get("balance", 0)) + referral_bonus, 8)
                referrer["referral_earnings"] = round(float(referrer.get("referral_earnings", 0)) + referral_bonus, 8)

            save_db(db)
            new_balance = u["balance"]

        elif action == "purchase_reject":
            purchase["status"] = "rejected"
            purchase["rejected_at"] = now_str()
            save_db(db)

    if action == "purchase_accept":
        text = (
            f"🎉 <b>تم قبول العملية {purchase_id}</b>\n\n"
            f"تم إضافة <b>{money(purchase['usd'])}$</b> إلى رصيدك.\n"
            f"💰 رصيدك الحالي: <b>{money(new_balance)}$</b>"
        )
        safe_send(purchase["user_id"], text, reply_markup=main_inline_keyboard())

        if referral_bonus > 0 and referrer_id:
            safe_send(
                int(referrer_id),
                f"🎁 كسبت <b>{money(referral_bonus)}$</b> عمولة إحالة!\n"
                f"أحد أصدقائك أتم عملية شحن عبر رابطك.",
                reply_markup=main_inline_keyboard()
            )

        bot.answer_callback_query(call.id, "تم قبول العملية.")

    elif action == "purchase_reject":
        text = (
            f"❌ <b>تم رفض العملية {purchase_id}</b>\n\n"
            "لم تتم إضافة أي رصيد.\n"
            "للتواصل مع الدعم: <b>@hwa_y55</b>"
        )
        safe_send(purchase["user_id"], text, reply_markup=main_inline_keyboard())
        bot.answer_callback_query(call.id, "تم رفض العملية.")

    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass


def handle_withdraw_start(message, uid):
    with db_lock:
        opened = db["settings"].get("withdrawals_open", False)

    if not opened:
        try:
            bot.edit_message_text(
                "🔒 <b>السحب مغلق حالياً.</b>\n\nانتظر حتى يوم السبت لفتح السحب.",
                message.chat.id,
                message.message_id,
                reply_markup=back_inline()
            )
        except Exception:
            safe_send(message.chat.id, "🔒 السحب مغلق حالياً.", reply_markup=main_inline_keyboard())
        return

    with db_lock:
        u = db["users"].get(str(uid))
    balance = float(u.get("balance", 0)) if u else 0.0

    if balance <= 0:
        try:
            bot.edit_message_text(
                "❌ ليس لديك رصيد متاح للسحب.",
                message.chat.id,
                message.message_id,
                reply_markup=back_inline()
            )
        except Exception:
            safe_send(message.chat.id, "❌ ليس لديك رصيد.", reply_markup=main_inline_keyboard())
        return

    set_state(uid, "withdraw_amount")

    try:
        bot.edit_message_text(
            f"💸 <b>سحب الدولار</b>\n\n"
            f"رصيدك: <b>{money(balance)}$</b>\n\n"
            "أرسل عدد الدولارات التي تريد سحبها.\n"
            "مثال: <code>1.27</code>",
            message.chat.id,
            message.message_id,
            reply_markup=back_inline()
        )
    except Exception:
        safe_send(
            message.chat.id,
            f"💸 رصيدك: {money(balance)}$\nأرسل المبلغ المراد سحبه:"
        )


def process_withdraw_amount_text(message):
    uid = message.from_user.id
    amount = normalize_usd(message.text)
    with db_lock:
        u = db["users"].get(str(uid))
    balance = float(u.get("balance", 0)) if u else 0.0

    if amount is None:
        safe_send(message.chat.id, "❌ أدخل مبلغاً صحيحاً. مثال: <code>1.27</code>")
        return

    if amount > balance:
        safe_send(message.chat.id, f"❌ رصيدك غير كافٍ. رصيدك: <b>{money(balance)}$</b>")
        return

    set_state(uid, "withdraw_binance", {"usd": amount})
    safe_send(
        message.chat.id,
        f"💵 المبلغ: <b>{money(amount)}$</b>\n\nأرسل الآن <b>Binance ID</b> الخاص بك:"
    )


def process_withdraw_binance_text(message):
    uid = message.from_user.id
    st = get_state(uid)
    if not st:
        send_main_menu(message.chat.id)
        return

    binance_id = message.text.strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{3,100}", binance_id):
        safe_send(message.chat.id, "❌ Binance ID غير صالح. أرسله بدون فراغات.")
        return

    amount = float(st["data"]["usd"])

    with db_lock:
        u = db["users"].get(str(uid))
        if not u:
            clear_state(uid)
            safe_send(message.chat.id, "❌ تعذر العثور على حسابك.")
            return

        current_balance = float(u.get("balance", 0))
        if amount > current_balance:
            clear_state(uid)
            safe_send(message.chat.id, "❌ رصيدك غير كافٍ.", reply_markup=main_inline_keyboard())
            return

        operation_id = next_operation_id()
        withdrawal_id = f"#{operation_id}"

        u["balance"] = round(current_balance - amount, 8)
        u["withdrawal_count"] = u.get("withdrawal_count", 0) + 1

        withdrawal = {
            "id": withdrawal_id,
            "user_id": uid,
            "usd": amount,
            "binance_id": binance_id,
            "created_at": now_str(),
            "status": "pending",
        }
        db["withdrawals"][withdrawal_id] = withdrawal
        save_db(db)

    clear_state(uid)

    safe_send(
        message.chat.id,
        f"✅ تم تسجيل طلب السحب <b>{withdrawal_id}</b>.\n\n"
        f"💵 المبلغ: <b>{money(amount)}$</b>\n"
        f"💰 الرصيد المتبقي: <b>{money(u['balance'])}$</b>\n\n"
        "⏳ سيتم إرسال الدولار خلال 1 دقيقة إلى 5 ساعات.\n"
        "📩 ستتلقى إشعاراً عند إتمام التحويل.\n"
        "⚠️ للدعم: <b>@hwa_y55</b>",
        reply_markup=main_inline_keyboard()
    )

    send_withdrawal_to_admin(withdrawal)


def send_withdrawal_to_admin(withdrawal):
    u = get_user_by_id(withdrawal["user_id"])
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("بروفايل العضو", url=f"tg://user?id={withdrawal['user_id']}", style="success"))

    text = (
        "💸 <b>طلب سحب جديد</b>\n\n"
        f"🔢 العملية: <b>{withdrawal['id']}</b>\n"
        f"👤 العضو: {user_display(u)}\n"
        f"💵 المبلغ: <b>{money(withdrawal['usd'])}$</b>\n"
        f"🆔 Binance ID: <code>{withdrawal['binance_id']}</code>\n"
        f"🕒 الوقت: <b>{withdrawal['created_at']}</b>\n\n"
        "⚠️ تم خصم المبلغ من رصيد العضو مباشرةً."
    )
    safe_send(ADMIN_ID, text, reply_markup=kb)


def process_other_country_name(message):
    uid = message.from_user.id
    country = message.text.strip()
    set_state(uid, "other_currency_code", {"country": country})
    safe_send(
        message.chat.id,
        f"✅ البلد: <b>{country}</b>\n\n"
        "السؤال 2️⃣: ما هو <b>رمز عملتك</b>؟\n\n"
        "مثال: الجزائر = <code>DZD</code> | مصر = <code>EGP</code> | السعودية = <code>SAR</code>\n\n"
        "أرسل رمز عملتك الآن:"
    )


def process_other_currency_code(message):
    uid = message.from_user.id
    st = get_state(uid)
    currency = message.text.strip().upper()
    data = st["data"]
    data["currency"] = currency
    set_state(uid, "other_payment_method", data)
    safe_send(
        message.chat.id,
        f"✅ رمز العملة: <b>{currency}</b>\n\n"
        "السؤال 3️⃣: ما هي <b>طريقة الدفع</b> المتاحة لديك؟\n\n"
        "مثال: محفظة إلكترونية، تحويل بنكي، كاش...\n\n"
        "أرسل طريقة الدفع:"
    )


def process_other_payment_method(message):
    uid = message.from_user.id
    st = get_state(uid)
    pay_method = message.text.strip()
    data = st["data"]
    data["payment_method"] = pay_method
    set_state(uid, "other_payment_details", data)
    safe_send(
        message.chat.id,
        f"✅ طريقة الدفع: <b>{pay_method}</b>\n\n"
        "السؤال 4️⃣: أرسل <b>معلومات الدفع</b> (رقم المحفظة، رقم الحساب، إلخ):"
    )


def process_other_payment_details(message):
    uid = message.from_user.id
    st = get_state(uid)
    details = message.text.strip()
    data = st["data"]
    data["payment_details"] = details
    set_state(uid, "other_usd_amount", data)
    safe_send(
        message.chat.id,
        f"✅ معلومات الدفع: <code>{details}</code>\n\n"
        "السؤال 5️⃣: كم دولاراً تريد شراءه؟\n\n"
        "مثال: <code>5</code> أو <code>10.50</code>"
    )


def process_other_usd_amount(message):
    uid = message.from_user.id
    st = get_state(uid)
    amount = normalize_usd(message.text)
    data = st["data"]

    if amount is None or amount <= 0:
        safe_send(message.chat.id, "❌ أرسل مبلغاً صحيحاً. مثال: <code>5</code>")
        return

    data["usd_amount"] = amount
    clear_state(uid)

    u = get_user_by_id(uid)

    text_to_admin = (
        "🌍 <b>طلب شراء - دول العالم</b>\n\n"
        f"👤 العضو: {user_display(u)}\n"
        f"🌐 البلد: <b>{data.get('country', '؟')}</b>\n"
        f"💰 العملة: <b>{data.get('currency', '؟')}</b>\n"
        f"💳 طريقة الدفع: <b>{data.get('payment_method', '؟')}</b>\n"
        f"📋 معلومات الدفع: <code>{data.get('payment_details', '؟')}</code>\n"
        f"💵 المبلغ المطلوب: <b>{money(amount)}$</b>"
    )

    safe_send(ADMIN_ID, text_to_admin)

    safe_send(
        message.chat.id,
        "✅ <b>تم استلام معلوماتك!</b>\n\n"
        f"💵 طلبت شراء: <b>{money(amount)}$</b>\n\n"
        "📩 تم إرسال طلبك إلى الأدمن.\n"
        "سيتواصل معك الأدمن قريباً لإتمام العملية.\n\n"
        "للتواصل المباشر: <b>@hwa_y55</b>",
        reply_markup=main_inline_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("adm_"))
def admin_callback(call):
    if not require_verified_callback(call):
        return
    uid = call.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(call.id, "❌ غير مسموح.")
        return

    bot.answer_callback_query(call.id)
    action = call.data

    if action == "adm_broadcast":
        set_state(uid, "admin_broadcast")
        try:
            bot.edit_message_text(
                "📢 أرسل الرسالة التي تريد إذاعتها:\n\n"
                "• نص\n• صورة\n• فيديو\n• ملف\n• صوت\n• ستيكر",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=back_inline("adm_home")
            )
        except Exception:
            pass

    elif action == "adm_flexy_phone":
        set_state(uid, "admin_change_phone")
        phone = db["settings"].get("flexy_phone", FLEXY_PHONE)
        try:
            bot.edit_message_text(
                f"📱 رقم الفليكسي الحالي: <code>{phone}</code>\n\n"
                "أرسل الرقم الجديد:\nمثال: <code>0696690509</code>",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=back_inline("adm_home")
            )
        except Exception:
            pass

    elif action == "adm_ccp_number":
        set_state(uid, "admin_change_ccp")
        ccp = db["settings"].get("ccp_number", CCP_NUMBER)
        try:
            bot.edit_message_text(
                f"🏦 رقم CCP الحالي: <code>{ccp}</code>\n\n"
                "أرسل الرقم الجديد:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=back_inline("adm_home")
            )
        except Exception:
            pass

    elif action == "adm_egypt_numbers":
        with db_lock:
            instapay = db["settings"].get("egypt_instapay_number", "غير مضبوط")
            vodafone = db["settings"].get("egypt_vodafone_number", "غير مضبوط")
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("تغيير إنستاباي", callback_data="adm_set_instapay", style="success"),
            types.InlineKeyboardButton("تغيير فودافون كاش", callback_data="adm_set_vodafone", style="success"),
            types.InlineKeyboardButton("رجوع", callback_data="adm_home", style="success"),
        )
        try:
            bot.edit_message_text(
                f"🇪🇬 <b>أرقام مصر</b>\n\n"
                f"💳 إنستاباي: <code>{instapay}</code>\n"
                f"📱 فودافون كاش: <code>{vodafone}</code>",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=kb
            )
        except Exception:
            pass

    elif action == "adm_set_instapay":
        set_state(uid, "admin_egypt_instapay")
        try:
            bot.edit_message_text(
                "✏️ أرسل رقم إنستاباي الجديد:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=back_inline("adm_egypt_numbers")
            )
        except Exception:
            pass

    elif action == "adm_set_vodafone":
        set_state(uid, "admin_egypt_vodafone")
        try:
            bot.edit_message_text(
                "✏️ أرسل رقم فودافون كاش الجديد:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=back_inline("adm_egypt_numbers")
            )
        except Exception:
            pass

    elif action == "adm_toggle_withdraw":
        with db_lock:
            current = db["settings"].get("withdrawals_open", False)
            db["settings"]["withdrawals_open"] = not current
            save_db(db)
            new_status = db["settings"]["withdrawals_open"]
        status = "🟢 مفتوح" if new_status else "🔴 مغلق"
        send_admin_panel(call.message.chat.id, call.message.message_id)

    elif action == "adm_toggle_ccp":
        with db_lock:
            current = db["settings"].get("ccp_enabled", True)
            db["settings"]["ccp_enabled"] = not current
            save_db(db)
        send_admin_panel(call.message.chat.id, call.message.message_id)

    elif action == "adm_toggle_egypt":
        with db_lock:
            current = db["settings"].get("egypt_enabled", True)
            db["settings"]["egypt_enabled"] = not current
            save_db(db)
        send_admin_panel(call.message.chat.id, call.message.message_id)

    elif action == "adm_check_member":
        set_state(uid, "admin_check_member")
        try:
            bot.edit_message_text(
                "🔎 أرسل Telegram ID للعضو الذي تريد فحصه:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=back_inline("adm_home")
            )
        except Exception:
            pass


def process_change_phone_text(message):
    phone = re.sub(r"\s+", "", message.text.strip())
    if not re.fullmatch(r"0\d{9}", phone):
        safe_send(message.chat.id, "❌ الرقم غير صالح. مثال: <code>0696690509</code>")
        return
    with db_lock:
        db["settings"]["flexy_phone"] = phone
        save_db(db)
    clear_state(message.from_user.id)
    safe_send(message.chat.id, f"✅ تم تغيير رقم الفليكسي إلى: <code>{phone}</code>")
    send_admin_panel(message.chat.id)


def process_change_ccp_text(message):
    ccp = message.text.strip().replace(" ", "")
    if not re.fullmatch(r"\d{10,25}", ccp):
        safe_send(message.chat.id, "❌ الرقم غير صالح. أرسل رقم CCP صحيح.")
        return
    with db_lock:
        db["settings"]["ccp_number"] = ccp
        save_db(db)
    clear_state(message.from_user.id)
    safe_send(message.chat.id, f"✅ تم تغيير رقم CCP إلى: <code>{ccp}</code>")
    send_admin_panel(message.chat.id)


def process_egypt_instapay_text(message):
    number = message.text.strip()
    with db_lock:
        db["settings"]["egypt_instapay_number"] = number
        save_db(db)
    clear_state(message.from_user.id)
    safe_send(message.chat.id, f"✅ تم تغيير رقم إنستاباي إلى: <code>{number}</code>")
    send_admin_panel(message.chat.id)


def process_egypt_vodafone_text(message):
    number = message.text.strip()
    with db_lock:
        db["settings"]["egypt_vodafone_number"] = number
        save_db(db)
    clear_state(message.from_user.id)
    safe_send(message.chat.id, f"✅ تم تغيير رقم فودافون كاش إلى: <code>{number}</code>")
    send_admin_panel(message.chat.id)


def process_check_member_text(message):
    text = message.text.strip()
    if not text.isdigit():
        safe_send(message.chat.id, "❌ أرسل Telegram ID رقمي فقط.")
        return
    user_id = int(text)
    clear_state(message.from_user.id)
    show_member_admin(message, user_id)


def show_member_admin(message, user_id):
    with db_lock:
        u = db["users"].get(str(user_id))
    if not u:
        safe_send(message.chat.id, "❌ لم أجد هذا العضو في قاعدة البيانات.")
        send_admin_panel(message.chat.id)
        return

    username = f"@{u['username']}" if u.get("username") else "بدون يوزرنيم"
    name = (u.get("first_name", "") + " " + u.get("last_name", "")).strip() or "بدون اسم"
    referred_by = u.get("referred_by", "لا يوجد")

    text = (
        "🔎 <b>معلومات العضو</b>\n\n"
        f"🆔 ID: <code>{u['id']}</code>\n"
        f"🔹 Username: <b>{username}</b>\n"
        f"👤 الاسم: <b>{name}</b>\n"
        f"💰 الرصيد: <b>{money(u.get('balance', 0))}$</b>\n"
        f"🔗 عائد إحالات: <b>{money(u.get('referral_earnings', 0))}$</b>\n"
        f"👥 محيل: <b>{referred_by}</b>\n"
        f"🕒 تاريخ الانضمام: <b>{u.get('joined_at', '؟')}</b>\n"
        f"🛒 شراء ناجح: <b>{u.get('successful_purchases', 0)}</b>\n"
        f"📦 محاولات شراء: <b>{u.get('purchase_count', 0)}</b>\n"
        f"💸 سحب ناجح: <b>{u.get('successful_withdrawals', 0)}</b>\n"
        f"📋 طلبات سحب: <b>{u.get('withdrawal_count', 0)}</b>"
    )

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("زيادة رصيد", callback_data=f"member_add:{user_id}", style="success"),
        types.InlineKeyboardButton("نقص رصيد", callback_data=f"member_sub:{user_id}", style="success"),
    )
    kb.add(types.InlineKeyboardButton("لوحة الأدمن", callback_data="adm_home", style="danger"))

    safe_send(message.chat.id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("member_"))
def member_callback(call):
    if not require_verified_callback(call):
        return
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ غير مسموح.")
        return

    parts = call.data.split(":")
    action = parts[0]
    uid = parts[1] if len(parts) > 1 else ""

    bot.answer_callback_query(call.id)

    if action == "member_add":
        set_state(call.from_user.id, "admin_member_add", {"user_id": uid})
        try:
            bot.edit_message_text(
                f"➕ أرسل عدد الدولارات التي تريد إضافتها للعضو <code>{uid}</code>:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=back_inline("adm_home")
            )
        except Exception:
            pass

    elif action == "member_sub":
        set_state(call.from_user.id, "admin_member_sub", {"user_id": uid})
        try:
            bot.edit_message_text(
                f"➖ أرسل عدد الدولارات التي تريد خصمها من العضو <code>{uid}</code>:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=back_inline("adm_home")
            )
        except Exception:
            pass


def admin_member_change(message, add=True):
    st = get_state(message.from_user.id)
    if not st:
        send_admin_panel(message.chat.id)
        return

    uid = st["data"]["user_id"]
    amount = normalize_usd(message.text)

    if amount is None:
        safe_send(message.chat.id, "❌ أرسل مبلغاً صحيحاً. مثال: <code>3.50</code>")
        return

    with db_lock:
        u = db["users"].get(str(uid))
        if not u:
            clear_state(message.from_user.id)
            safe_send(message.chat.id, "❌ العضو غير موجود.")
            send_admin_panel(message.chat.id)
            return

        old = float(u.get("balance", 0))
        if add:
            u["balance"] = round(old + amount, 8)
            action_word = "إضافة"
        else:
            if amount > old:
                safe_send(message.chat.id, f"❌ لا يمكن خصم {money(amount)}$. رصيد العضو: {money(old)}$")
                return
            u["balance"] = round(old - amount, 8)
            action_word = "خصم"

        save_db(db)
        new_balance = u["balance"]

    clear_state(message.from_user.id)
    safe_send(
        message.chat.id,
        f"✅ تم {action_word} <b>{money(amount)}$</b>.\nالرصيد الجديد: <b>{money(new_balance)}$</b>"
    )
    send_admin_panel(message.chat.id)

    notif = (
        f"💰 تم إضافة <b>{money(amount)}$</b> إلى رصيدك من الأدمن.\n"
        f"رصيدك الحالي: <b>{money(new_balance)}$</b>"
        if add else
        f"➖ تم خصم <b>{money(amount)}$</b> من رصيدك من الأدمن.\n"
        f"رصيدك الحالي: <b>{money(new_balance)}$</b>"
    )
    safe_send(int(uid), notif, reply_markup=main_inline_keyboard())


def start_broadcast(message):
    if not is_admin(message.from_user.id):
        return
    set_state(message.from_user.id, "admin_broadcast")
    safe_send(
        message.chat.id,
        "📢 أرسل الرسالة التي تريد إذاعتها:"
    )


def broadcast_copy(message):
    if not is_admin(message.from_user.id):
        return
    with db_lock:
        ids = list(db["users"].keys())
    clear_state(message.from_user.id)
    safe_send(message.chat.id, f"📢 بدأت الإذاعة إلى <b>{len(ids)}</b> عضو...")

    sent = 0
    failed = 0
    for uid_str in ids:
        try:
            bot.copy_message(chat_id=int(uid_str), from_chat_id=message.chat.id, message_id=message.message_id)
            sent += 1
            time.sleep(0.05)
        except Exception:
            failed += 1

    safe_send(
        message.chat.id,
        f"✅ انتهت الإذاعة.\n📨 تم: <b>{sent}</b>\n❌ فشل: <b>{failed}</b>"
    )
    send_admin_panel(message.chat.id)


@bot.message_handler(content_types=["document", "video", "audio", "voice", "sticker", "animation"])
def other_content_handler(message):
    uid = message.from_user.id
    if not is_user_verified(uid):
        send_captcha(message.chat.id, uid)
        return
    if is_admin(uid):
        st = get_state(uid)
        if st and st["state"] == "admin_broadcast":
            broadcast_copy(message)
            return
    safe_send(message.chat.id, "❌ هذا النوع من الرسائل غير مدعوم حالياً.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("captcha:"))
def captcha_callback(call):
    try:
        _, nonce, selected_text = call.data.split(":", 2)
        selected = int(selected_text)
    except (ValueError, TypeError):
        bot.answer_callback_query(call.id, "تحقق غير صالح.")
        return

    uid = str(call.from_user.id)
    with db_lock:
        user = db["users"].get(uid)
        if not user or user.get("captcha_verified", True):
            bot.answer_callback_query(call.id, "تم التحقق أو انتهت المحاولة.")
            return
        if nonce != user.get("captcha_nonce") or time.time() > float(user.get("captcha_expires", 0) or 0):
            bot.answer_callback_query(call.id, "انتهى الوقت، أنشأنا لك اختباراً جديداً.")
            expired = True
        else:
            expired = False
            if selected == int(user.get("captcha_answer", -1)):
                user["captcha_verified"] = True
                user.pop("captcha_nonce", None)
                user.pop("captcha_answer", None)
                user.pop("captcha_expires", None)
                user.pop("captcha_attempts", None)
                user.pop("captcha_blocked_until", None)
                save_db(db)
                verified = True
            else:
                verified = False
                attempts = int(user.get("captcha_attempts", 0) or 0) + 1
                user["captcha_attempts"] = attempts
                user.pop("captcha_nonce", None)
                user.pop("captcha_answer", None)
                user.pop("captcha_expires", None)
                if attempts >= CAPTCHA_MAX_ATTEMPTS:
                    user["captcha_blocked_until"] = time.time() + CAPTCHA_LOCK_SECONDS
                save_db(db)

    if expired:
        bot.answer_callback_query(call.id, "انتهى الوقت.")
        send_captcha(call.message.chat.id, call.from_user.id)
        return
    if verified:
        bot.answer_callback_query(call.id, "✅ تم التحقق بنجاح!")
        try:
            bot.edit_message_text(
                "✅ <b>تم التحقق بنجاح!</b>\nتقدر الآن تستعمل البوت.",
                call.message.chat.id,
                call.message.message_id,
            )
        except Exception:
            pass
        send_main_menu(call.message.chat.id)
        return

    if int(user.get("captcha_attempts", 0) or 0) >= CAPTCHA_MAX_ATTEMPTS:
        bot.answer_callback_query(call.id, "تجاوزت المحاولات.")
        safe_send(call.message.chat.id, "🚫 محاولات كثيرة. استنى دقيقة ثم أرسل /start.")
    else:
        bot.answer_callback_query(call.id, "❌ الرمز غير صحيح، جرّب من جديد.")
        send_captcha(call.message.chat.id, call.from_user.id)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN environment variable. Add it in Railway Variables.")

    print("===================================")
    print("Flexy Dollar Bot is starting...")
    print("===================================")

    configure_commands()

    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)
        except Exception as e:
            print("Polling error:", repr(e))
            print("Retrying in 5 seconds...")
            time.sleep(5)


if __name__ == "__main__":
    main()

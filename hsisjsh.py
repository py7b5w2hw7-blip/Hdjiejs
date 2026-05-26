# bot.py
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
import sqlite3, time, threading, requests, random, string, os, json
from datetime import datetime
from config import *

# ========== БАЗА ДАННЫХ ==========
conn = sqlite3.connect('bot.db', check_same_thread=False)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
db_lock = threading.Lock()

cur.executescript('''
CREATE TABLE IF NOT EXISTS users(
    user_id TEXT PRIMARY KEY, username TEXT,
    first_seen INTEGER, last_seen INTEGER,
    balance INTEGER DEFAULT 0, channel_msg_id INTEGER
);
CREATE TABLE IF NOT EXISTS mirrors(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT UNIQUE, username TEXT,
    added_by TEXT, added_at INTEGER, is_active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS current_bot(
    id INTEGER PRIMARY KEY, token TEXT, username TEXT, updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS pending_payments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT, username TEXT, amount INTEGER,
    product TEXT, screenshot TEXT, timestamp INTEGER
);
CREATE TABLE IF NOT EXISTS user_sessions(user_id TEXT PRIMARY KEY, step TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS admin_sessions(user_id TEXT PRIMARY KEY, step TEXT);
CREATE TABLE IF NOT EXISTS user_stats(user_id TEXT PRIMARY KEY, ref_code TEXT, earned INTEGER DEFAULT 0, ref_clicks INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS referals(code TEXT PRIMARY KEY, owner_id TEXT, earnings INTEGER DEFAULT 0, clicks INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS promo_codes(code TEXT PRIMARY KEY, discount INTEGER, uses_left INTEGER, created_at INTEGER, is_active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS user_promo(user_id TEXT PRIMARY KEY, discount INTEGER, expires_at INTEGER);
''')
conn.commit()

def db(q, p=()):
    with db_lock:
        cur.execute(q, p); conn.commit(); return cur
def dbf(q, p=()):
    with db_lock:
        cur.execute(q, p); return cur.fetchone()
def dbfa(q, p=()):
    with db_lock:
        cur.execute(q, p); return cur.fetchall()

# ========== ЛОГГЕР ==========
def log_to_admin(text, photo=None, reply_markup=None):
    try:
        if photo:
            data = {"chat_id": ADMIN_ID, "photo": photo, "caption": text, "parse_mode": "HTML"}
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup)
            requests.post(f"https://api.telegram.org/bot{LOGGER_BOT_TOKEN}/sendPhoto", data=data, timeout=15)
        else:
            data = {"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML"}
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup)
            requests.post(f"https://api.telegram.org/bot{LOGGER_BOT_TOKEN}/sendMessage", data=data, timeout=15)
    except: pass

# ========== ПОЛЬЗОВАТЕЛИ ==========
def register_user(uid, uname, ref=None):
    if not dbf("SELECT 1 FROM users WHERE user_id=?", (uid,)):
        db("INSERT INTO users(user_id,username,first_seen,last_seen,balance) VALUES(?,?,?,?,0)",
           (uid, uname, int(time.time()), int(time.time())))
        if ref and ref != uid:
            db("UPDATE referals SET clicks=clicks+1 WHERE code=?", (ref,))
            db("INSERT OR IGNORE INTO user_stats(user_id,ref_code,earned,ref_clicks) VALUES(?,?,0,0)", (uid, ref))
    else:
        db("UPDATE users SET username=?,last_seen=? WHERE user_id=?", (uname, int(time.time()), uid))

def get_balance(uid):
    r = dbf("SELECT balance FROM users WHERE user_id=?", (uid,))
    return r[0] if r else 0

def add_balance(uid, amt):
    db("UPDATE users SET balance=balance+? WHERE user_id=?", (amt, uid))

# ========== РЕФЕРАЛКА ==========
def get_ref_link(uid, bot_uname):
    r = dbf("SELECT ref_code FROM user_stats WHERE user_id=?", (uid,))
    if r and r[0]:
        code = r[0]
    else:
        code = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
        db("INSERT OR REPLACE INTO user_stats(user_id,ref_code,earned,ref_clicks) VALUES(?,?,0,0)", (uid, code))
        db("INSERT OR IGNORE INTO referals(code,owner_id,earnings,clicks) VALUES(?,?,0,0)", (code, uid))
    return f"https://t.me/{bot_uname}?start=ref_{uid}"

# ========== ПРОМОКОДЫ ==========
def apply_promo(uid, code):
    r = dbf("SELECT discount,uses_left FROM promo_codes WHERE code=? AND is_active=1 AND uses_left>0", (code.upper(),))
    if not r: return False, 0
    db("INSERT OR REPLACE INTO user_promo(user_id,discount,expires_at) VALUES(?,?,?)", (uid, r[0], int(time.time())+3600))
    db("UPDATE promo_codes SET uses_left=uses_left-1 WHERE code=?", (code.upper(),))
    return True, r[0]

def get_discount(uid):
    r = dbf("SELECT discount FROM user_promo WHERE user_id=? AND expires_at>?", (uid, int(time.time())))
    return r[0] if r else 0

# ========== КАЗИНО ==========
def play_mines(bet):
    if random.random() < 0.85:
        return int(bet * random.choice([1.5, 2.0, 2.5])), "🎉 выигрыш!"
    return 0, "💥 мина взорвалась"

def play_rocket(bet):
    if random.random() < 0.85:
        m = random.choice([1.5, 2.0, 2.5, 3.0])
        return int(bet * m), f"🚀 x{m} → выигрыш!"
    return 0, "💥 ракета взорвалась"

def open_case(bet):
    if random.random() < 0.85:
        mult = random.choice([0.5, 1.5, 3.0, 5.0])
        return int(bet * mult), f"📦 выпал скин +{int(bet * mult)}₽"
    return 0, "ничего не выпало"

# ========== ЗЕРКАЛА ==========
def get_current():
    r = dbf("SELECT token,username FROM current_bot WHERE id=1")
    return (r[0], r[1]) if r else (WORKER_BOT_TOKEN, "worker")

def set_current(token, username):
    db("DELETE FROM current_bot WHERE id=1")
    db("INSERT INTO current_bot(id,token,username,updated_at) VALUES(1,?,?,?)", (token, username, int(time.time())))
    log_to_admin(f"🔄 текущий бот: @{username}")

def check_alive(token):
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
        if r.json().get('ok'):
            return True, r.json()['result']['username']
        return False, None
    except:
        return False, None

def add_mirror(token, username, added_by):
    db("INSERT OR REPLACE INTO mirrors(token,username,added_by,added_at,is_active) VALUES(?,?,?,?,1)",
       (token, username, added_by, int(time.time())))

def delete_mirror_by_id(mid):
    db("UPDATE mirrors SET is_active=0 WHERE id=?", (mid,))

def get_mirrors():
    return dbfa("SELECT id,token,username FROM mirrors WHERE is_active=1 ORDER BY added_at ASC")

def get_mirror_by_id(mid):
    return dbf("SELECT id,token,username FROM mirrors WHERE id=? AND is_active=1", (mid,))

# ========== РОТАЦИЯ ==========
def rotate_worker():
    current_token, current_name = get_current()
    log_to_admin(f"💀 бот @{current_name} упал! ищу замену…")
    for mid, token, uname in get_mirrors():
        if token == current_token: continue
        alive, real_uname = check_alive(token)
        if alive:
            set_current(token, real_uname or uname)
            log_to_admin(f"✅ авто-ротация → @{real_uname or uname}")
            return True
    log_to_admin("❌ нет живых зеркал")
    return False

def monitor_loop():
    while True:
        time.sleep(300)
        token, name = get_current()
        alive, _ = check_alive(token)
        if not alive:
            rotate_worker()

# ========== БОТ-ПРОДАЖ (РАБОЧИЙ) ==========
worker_bot = telebot.TeleBot(WORKER_BOT_TOKEN, threaded=True)

def main_menu():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🛒 магазин", callback_data="shop"),
        InlineKeyboardButton("🎰 казино", callback_data="casino"),
        InlineKeyboardButton("⭐ отзывы", callback_data="reviews"),
        InlineKeyboardButton("📈 рефералка", callback_data="referral"),
        InlineKeyboardButton("🎟 промокод", callback_data="promo"),
        InlineKeyboardButton("👤 профиль", callback_data="profile")
    )
    return kb

@worker_bot.message_handler(commands=['start'])
def worker_start(m):
    uid, uname = str(m.from_user.id), m.from_user.username or "no_username"
    register_user(uid, uname)
    worker_bot.send_message(m.chat.id, "🍼 Детское питание Shop\n\nвыбери действие:", parse_mode='HTML', reply_markup=main_menu())

@worker_bot.callback_query_handler(func=lambda c: True)
def worker_cb(call):
    uid, uname = str(call.from_user.id), call.from_user.username or "no_username"
    cid, mid = call.message.chat.id, call.message.message_id

    if call.data == "shop":
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton("👶 5-10 лет — 600₽", callback_data="buy_5_10"),
               InlineKeyboardButton("🧒 10-18 лет — 450₽", callback_data="buy_10_18"),
               InlineKeyboardButton("🔙 назад", callback_data="back"))
        worker_bot.edit_message_text("📦 выбери категорию:", cid, mid, reply_markup=kb)

    elif call.data in ("buy_5_10", "buy_10_18"):
        is510 = call.data == "buy_5_10"
        price = PRICE_5_10 if is510 else PRICE_10_18
        photo = PHOTO_5_10 if is510 else PHOTO_10_18
        cat = "👶 5-10 лет" if is510 else "🧒 10-18 лет"
        url = CRYPTO_PAYMENT_600 if is510 else CRYPTO_PAYMENT_450
        caption = f"{cat}\n💰 цена: {price}₽\n\n💳 после оплаты нажми «ОПЛАТИЛ»\n⚠️ в комментарии: @{uname}"
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("💳 ОПЛАТИТЬ", url=url),
               InlineKeyboardButton("✅ ОПЛАТИЛ", callback_data=f"askscr_{price}_{'510' if is510 else '1018'}"),
               InlineKeyboardButton("🔙 назад", callback_data="shop"))
        try:
            worker_bot.edit_message_media(InputMediaPhoto(photo, caption=caption, parse_mode='HTML'), cid, mid, reply_markup=kb)
        except:
            worker_bot.edit_message_text(caption, cid, mid, reply_markup=kb)

    elif call.data.startswith("askscr_"):
        _, price, prod = call.data.split("_")
        db("INSERT OR REPLACE INTO user_sessions VALUES(?,?,?)", (uid, "await_scr", f"{price}_{prod}"))
        worker_bot.answer_callback_query(call.id)
        worker_bot.send_message(cid, "📸 отправь скриншот чека об оплате:")

    elif call.data == "reviews":
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("⭐ канал с отзывами", url=REVIEWS_CHANNEL), InlineKeyboardButton("🔙 назад", callback_data="back"))
        worker_bot.edit_message_text("⭐ отзывы наших клиентов", cid, mid, reply_markup=kb)

    elif call.data == "referral":
        bot_username = worker_bot.get_me().username
        link = get_ref_link(uid, bot_username)
        r = dbf("SELECT earned, ref_clicks FROM user_stats WHERE user_id=?", (uid,))
        earned, clicks = (r[0], r[1]) if r else (0, 0)
        text = f"📈 рефералка\n\n<code>{link}</code>\n\n💰 заработано: {earned}₽\n👥 переходов: {clicks}\n🎁 40% с пополнений"
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔙 назад", callback_data="back"))
        worker_bot.edit_message_text(text, cid, mid, parse_mode='HTML', reply_markup=kb)

    elif call.data == "promo":
        db("INSERT OR REPLACE INTO user_sessions VALUES(?,?,?)", (uid, "await_promo", ""))
        worker_bot.answer_callback_query(call.id)
        worker_bot.delete_message(cid, mid)
        worker_bot.send_message(cid, "🎟 введи промокод:")

    elif call.data == "profile":
        balance = get_balance(uid)
        r = dbf("SELECT earned FROM user_stats WHERE user_id=?", (uid,))
        earned = r[0] if r else 0
        text = f"👤 профиль\n\n🆔 {uid}\n👤 @{uname}\n💰 баланс: {balance}₽\n💸 реф. заработок: {earned}₽"
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("💰 пополнить", callback_data="deposit"), InlineKeyboardButton("🔙 назад", callback_data="back"))
        worker_bot.edit_message_text(text, cid, mid, reply_markup=kb)

    elif call.data == "deposit":
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton("💎 CRYPTOBOT", callback_data="dep_crypto"),
               InlineKeyboardButton("💳 DONATIONALERTS", callback_data="dep_da"),
               InlineKeyboardButton("🔙 назад", callback_data="profile"))
        worker_bot.edit_message_text("💰 пополнить баланс\n\nвыбери способ:", cid, mid, reply_markup=kb)

    elif call.data == "dep_crypto":
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(InlineKeyboardButton("10$ (~1000₽)", callback_data="dc_10"),
               InlineKeyboardButton("20$ (~2000₽)", callback_data="dc_20"),
               InlineKeyboardButton("50$ (~5000₽)", callback_data="dc_50"),
               InlineKeyboardButton("✏️ своя сумма", callback_data="dc_custom"),
               InlineKeyboardButton("🔙 назад", callback_data="deposit"))
        worker_bot.edit_message_text("💰 CRYPTOBOT\n\nвыбери сумму:", cid, mid, reply_markup=kb)

    elif call.data.startswith("dc_"):
        val = call.data[3:]
        if val == "custom":
            db("INSERT OR REPLACE INTO user_sessions VALUES(?,?,?)", (uid, "await_custom_dep", ""))
            worker_bot.answer_callback_query(call.id)
            worker_bot.delete_message(cid, mid)
            worker_bot.send_message(cid, "✏️ введи сумму в рублях (мин. 100₽):")
            return
        rub = int(val) * USDT_RATE
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton(f"💳 ОПЛАТИТЬ {rub}₽", url=CRYPTO_PAYMENT_600),
               InlineKeyboardButton("✅ ОПЛАТИЛ", callback_data=f"askdepscr_{rub}_crypto"),
               InlineKeyboardButton("🔙 назад", callback_data="dep_crypto"))
        worker_bot.edit_message_text(f"💰 пополнение {rub}₽ ({val} USDT)\n⚠️ в комментарии: @{uname}", cid, mid, reply_markup=kb)

    elif call.data == "dep_da":
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("💳 ОПЛАТИТЬ", url=f"https://www.donationalerts.com/r/{DONATIONALERTS_NICK}"),
               InlineKeyboardButton("📸 ОТПРАВИТЬ СКРИНШОТ", callback_data="askdepscr_0_da"),
               InlineKeyboardButton("🔙 назад", callback_data="deposit"))
        worker_bot.edit_message_text("💰 DONATIONALERTS\n\n1. оплати\n2. укажи в комментарии @" + uname + "\n3. отправь скриншот", cid, mid, reply_markup=kb)

    elif call.data.startswith("askdepscr_"):
        _, rub, src = call.data.split("_")
        db("INSERT OR REPLACE INTO user_sessions VALUES(?,?,?)", (uid, "await_dep_scr", f"{rub}_{src}"))
        worker_bot.answer_callback_query(call.id)
        worker_bot.send_message(cid, "📸 отправь скриншот чека пополнения:")

    elif call.data == "casino":
        balance = get_balance(uid)
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(InlineKeyboardButton("💣 mines", callback_data="g_mines"),
               InlineKeyboardButton("🚀 rocket", callback_data="g_rocket"),
               InlineKeyboardButton("📦 кейсы", callback_data="g_case"),
               InlineKeyboardButton("🔙 назад", callback_data="back"))
        worker_bot.edit_message_text(f"🎰 казино\n💰 баланс: {balance}₽", cid, mid, reply_markup=kb)

    elif call.data.startswith("g_"):
        game = call.data[2:]
        balance = get_balance(uid)
        if balance < 10:
            worker_bot.answer_callback_query(call.id, "❌ пополни баланс", show_alert=True)
            return
        kb = InlineKeyboardMarkup(row_width=3)
        kb.add(InlineKeyboardButton("10₽", callback_data=f"p_{game}_10"),
               InlineKeyboardButton("50₽", callback_data=f"p_{game}_50"),
               InlineKeyboardButton("100₽", callback_data=f"p_{game}_100"),
               InlineKeyboardButton("🔙 назад", callback_data="casino"))
        worker_bot.edit_message_text(f"🎲 {game}\n💰 баланс: {balance}₽", cid, mid, reply_markup=kb)

    elif call.data.startswith("p_"):
        _, game, bet_s = call.data.split("_")
        bet = int(bet_s)
        balance = get_balance(uid)
        if balance < bet:
            worker_bot.answer_callback_query(call.id, "❌ недостаточно средств", show_alert=True)
            return
        add_balance(uid, -bet)
        if game == "mines":
            win, msg = play_mines(bet)
        elif game == "rocket":
            win, msg = play_rocket(bet)
        else:
            win, msg = open_case(bet)
        if win > 0:
            add_balance(uid, win)
        new_balance = get_balance(uid)
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔄 ещё раз", callback_data=f"g_{game}"),
               InlineKeyboardButton("🔙 в казино", callback_data="casino"))
        worker_bot.edit_message_text(f"🎮 {game} | ставка: {bet}₽\n\n{msg}\n💰 баланс: {new_balance}₽", cid, mid, reply_markup=kb)

    elif call.data == "back":
        worker_bot.edit_message_text("🍼 Детское питание Shop\n\nвыбери действие:", cid, mid, reply_markup=main_menu())

# ========== ОБРАБОТЧИК ТЕКСТА ==========
@worker_bot.message_handler(func=lambda m: True, content_types=['text', 'photo'])
def worker_text(m):
    uid, uname = str(m.from_user.id), m.from_user.username or "no_username"
    row = dbf("SELECT step, data FROM user_sessions WHERE user_id=?", (uid,))
    step, sdata = (row[0], row[1]) if row else (None, None)

    if step == "await_promo":
        code = m.text.strip().upper()
        ok, disc = apply_promo(uid, code)
        worker_bot.reply_to(m, f"✅ промокод активирован! скидка {disc}%" if ok else "❌ неверный промокод")
        db("DELETE FROM user_sessions WHERE user_id=?", (uid,))

    elif step == "await_custom_dep":
        try:
            rub = int(m.text.strip())
            if rub < 100:
                worker_bot.reply_to(m, "❌ минимум 100₽")
                return
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton(f"💳 ОПЛАТИТЬ {rub}₽", url=CRYPTO_PAYMENT_600),
                   InlineKeyboardButton("✅ ОПЛАТИЛ", callback_data=f"askdepscr_{rub}_crypto"),
                   InlineKeyboardButton("🔙 назад", callback_data="dep_crypto"))
            worker_bot.send_message(m.chat.id, f"💰 пополнение {rub}₽\n⚠️ в комментарии: @{uname}", reply_markup=kb)
            db("DELETE FROM user_sessions WHERE user_id=?", (uid,))
        except:
            worker_bot.reply_to(m, "❌ введи число")

    elif step == "await_scr":
        if m.content_type != 'photo':
            worker_bot.reply_to(m, "❌ отправь фото чека")
            return
        price, prod = sdata.split("_")
        photo = m.photo[-1].file_id
        db("INSERT INTO pending_payments(user_id,username,amount,product,screenshot,timestamp) VALUES(?,?,?,?,?,?)",
           (uid, uname, price, prod, photo, int(time.time())))
        pid = dbf("SELECT last_insert_rowid()")[0]
        lab = "5-10 лет" if prod == "510" else "10-18 лет"
        log_to_admin(f"🛒 НОВАЯ ОПЛАТА\n👤 @{uname}\n📦 {lab}\n💰 {price}₽",
                     reply_markup={"inline_keyboard": [[{"text": "🎁 ВЫДАТЬ ДОСТУП", "callback_data": f"ga_{pid}"}]]})
        worker_bot.reply_to(m, "✅ скриншот отправлен!")
        db("DELETE FROM user_sessions WHERE user_id=?", (uid,))

    elif step == "await_dep_scr":
        if m.content_type != 'photo':
            worker_bot.reply_to(m, "❌ отправь фото чека")
            return
        rub, src = sdata.split("_")
        photo = m.photo[-1].file_id
        db("INSERT INTO pending_payments(user_id,username,amount,product,screenshot,timestamp) VALUES(?,?,?,?,?,?)",
           (uid, uname, rub, "deposit", photo, int(time.time())))
        pid = dbf("SELECT last_insert_rowid()")[0]
        src_label = "DonationAlerts" if src == "da" else "CryptoBot"
        log_to_admin(f"💳 ПОПОЛНЕНИЕ\n👤 @{uname}\n💰 {rub}₽\n🏦 {src_label}",
                     reply_markup={"inline_keyboard": [[{"text": "💰 ВЫДАТЬ БАЛАНС", "callback_data": f"gb_{pid}"}]]})
        worker_bot.reply_to(m, "✅ скриншот отправлен!")
        db("DELETE FROM user_sessions WHERE user_id=?", (uid,))

    elif m.content_type == 'photo':
        photo = m.photo[-1].file_id
        log_to_admin(f"📸 скриншот без сессии\n👤 @{uname}", photo=photo)
        worker_bot.reply_to(m, "✅ скриншот передан админу.")

# ========== БОТ-ЛОГГЕР (АДМИНКА) ==========
logger_bot = telebot.TeleBot(LOGGER_BOT_TOKEN)

def admin_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("📊 статистика", callback_data="a_stats"),
           InlineKeyboardButton("🪞 зеркала", callback_data="a_mirrors"),
           InlineKeyboardButton("⏳ оплаты", callback_data="a_pending"),
           InlineKeyboardButton("📢 рассылка", callback_data="a_spam"),
           InlineKeyboardButton("🎟 промокоды", callback_data="a_promos"))
    return kb

@logger_bot.message_handler(commands=['start', 'admin'])
def logger_start(m):
    if str(m.from_user.id) != ADMIN_ID:
        logger_bot.reply_to(m, "❌ доступ запрещён")
        return
    logger_bot.send_message(m.chat.id, "🔐 панель администратора", reply_markup=admin_kb())

# ========== КОМАНДА ДЛЯ САЙТА /current ==========
@logger_bot.message_handler(commands=['current'])
def cmd_current(m):
    if str(m.from_user.id) != ADMIN_ID:
        logger_bot.reply_to(m, "❌ доступ запрещён")
        return
    token, name = get_current()
    alive, real = check_alive(token)
    if alive and real:
        name = real
    logger_bot.reply_to(m, name)

# ========== ОСТАЛЬНЫЕ КОМАНДЫ ЛОГГЕРА ==========
@logger_bot.callback_query_handler(func=lambda c: True)
def admin_cb(call):
    if str(call.from_user.id) != ADMIN_ID:
        logger_bot.answer_callback_query(call.id, "доступ запрещён")
        return
    cid, mid = call.message.chat.id, call.message.message_id

    if call.data == "a_stats":
        u = dbf("SELECT COUNT(*) FROM users")[0]
        m = dbf("SELECT COUNT(*) FROM mirrors WHERE is_active=1")[0]
        p = dbf("SELECT COUNT(*) FROM pending_payments")[0]
        _, cur_name = get_current()
        logger_bot.edit_message_text(f"📊 статистика\n\n👥 пользователей: {u}\n🪞 зеркал: {m}\n⏳ оплат: {p}\n🤖 текущий: @{cur_name}", cid, mid, reply_markup=admin_kb())

    elif call.data == "a_mirrors":
        mirrors = get_mirrors()
        cur_tok, _ = get_current()
        if not mirrors:
            logger_bot.edit_message_text("🪞 зеркал нет", cid, mid, reply_markup=admin_kb())
            return
        kb = InlineKeyboardMarkup(row_width=1)
        for mid2, tok, uname in mirrors:
            alive, _ = check_alive(tok)
            status = "✅" if alive else "❌"
            is_cur = " 🔵" if tok == cur_tok else ""
            kb.add(InlineKeyboardButton(f"{status} @{uname}{is_cur}", callback_data=f"mir_{mid2}"))
        kb.add(InlineKeyboardButton("🔙 назад", callback_data="a_stats"))
        logger_bot.edit_message_text("🪞 выбери зеркало", cid, mid, reply_markup=kb)

    elif call.data.startswith("mir_"):
        m_id = int(call.data.split("_")[1])
        row = get_mirror_by_id(m_id)
        if not row:
            logger_bot.answer_callback_query(call.id, "не найдено", show_alert=True)
            return
        _, tok, uname = row
        cur_tok, _ = get_current()
        alive, _ = check_alive(tok)
        kb = InlineKeyboardMarkup(row_width=1)
        if tok != cur_tok:
            kb.add(InlineKeyboardButton("🔄 СДЕЛАТЬ ТЕКУЩИМ", callback_data=f"setmir_{m_id}"))
        kb.add(InlineKeyboardButton("❌ УДАЛИТЬ", callback_data=f"delmir_{m_id}"),
               InlineKeyboardButton("🔙 НАЗАД", callback_data="a_mirrors"))
        logger_bot.edit_message_text(f"🪞 @{uname}\nстатус: {'✅ живой' if alive else '❌ мёртвый'}", cid, mid, reply_markup=kb)

    elif call.data.startswith("setmir_"):
        m_id = int(call.data.split("_")[1])
        row = get_mirror_by_id(m_id)
        if not row:
            logger_bot.answer_callback_query(call.id, "не найдено", show_alert=True)
            return
        _, tok, uname = row
        alive, real = check_alive(tok)
        if not alive:
            logger_bot.answer_callback_query(call.id, "❌ бот мёртв", show_alert=True)
            return
        set_current(tok, real or uname)
        logger_bot.answer_callback_query(call.id, f"✅ переключено на @{real or uname}")
        logger_bot.edit_message_text(f"✅ текущий бот: @{real or uname}", cid, mid, reply_markup=admin_kb())

    elif call.data.startswith("delmir_"):
        m_id = int(call.data.split("_")[1])
        delete_mirror_by_id(m_id)
        logger_bot.answer_callback_query(call.id, "✅ удалено")
        logger_bot.edit_message_text("✅ зеркало удалено", cid, mid, reply_markup=admin_kb())

    elif call.data == "a_pending":
        rows = dbfa("SELECT id, user_id, username, amount, product FROM pending_payments ORDER BY timestamp DESC LIMIT 20")
        if not rows:
            logger_bot.edit_message_text("⏳ нет оплат", cid, mid, reply_markup=admin_kb())
            return
        kb = InlineKeyboardMarkup(row_width=1)
        for pid, uid, uname, amt, prod in rows:
            dt = datetime.now().strftime("%H:%M")
            kb.add(InlineKeyboardButton(f"[{dt}] @{uname} — {amt}₽", callback_data=f"pend_{pid}"))
        kb.add(InlineKeyboardButton("🔙 назад", callback_data="a_stats"))
        logger_bot.edit_message_text("⏳ ожидают оплаты:", cid, mid, reply_markup=kb)

    elif call.data.startswith("pend_"):
        pid = int(call.data.split("_")[1])
        row = dbf("SELECT user_id, username, amount, product, screenshot FROM pending_payments WHERE id=?", (pid,))
        if not row:
            logger_bot.answer_callback_query(call.id, "не найдено", show_alert=True)
            return
        uid, uname, amt, prod, scr = row
        lab = "5-10 лет" if prod == "510" else ("10-18 лет" if prod == "1018" else "пополнение")
        kb = InlineKeyboardMarkup()
        if prod == "deposit":
            kb.add(InlineKeyboardButton("💰 ВЫДАТЬ БАЛАНС", callback_data=f"gb_{pid}"))
        else:
            kb.add(InlineKeyboardButton("🎁 ВЫДАТЬ ДОСТУП", callback_data=f"ga_{pid}"))
        kb.add(InlineKeyboardButton("❌ ОТКЛОНИТЬ", callback_data=f"gd_{pid}"),
               InlineKeyboardButton("🔙 назад", callback_data="a_pending"))
        text = f"⏳ оплата #{pid}\n👤 @{uname}\n📦 {lab}\n💰 {amt}₽"
        if scr:
            try:
                logger_bot.send_photo(cid, scr, caption=text, parse_mode='HTML', reply_markup=kb)
                logger_bot.delete_message(cid, mid)
                return
            except:
                pass
        logger_bot.edit_message_text(text, cid, mid, reply_markup=kb)

    elif call.data.startswith("ga_"):
        pid = int(call.data.split("_")[1])
        row = dbf("SELECT user_id FROM pending_payments WHERE id=?", (pid,))
        if row:
            db("DELETE FROM pending_payments WHERE id=?", (pid,))
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🍼 ПОЛУЧИТЬ ДОСТУП", url=PAYMENT_CHANNEL))
            try:
                worker_bot.send_message(row[0], "✅ Оплата подтверждена!", reply_markup=kb)
                logger_bot.answer_callback_query(call.id, "✅ доступ выдан")
            except:
                logger_bot.answer_callback_query(call.id, "❌ ошибка")
        logger_bot.edit_message_text("✅ готово", cid, mid, reply_markup=admin_kb())

    elif call.data.startswith("gb_"):
        pid = int(call.data.split("_")[1])
        row = dbf("SELECT user_id, amount FROM pending_payments WHERE id=?", (pid,))
        if row:
            uid, amt = row
            db("DELETE FROM pending_payments WHERE id=?", (pid,))
            add_balance(uid, int(amt))
            try:
                worker_bot.send_message(uid, f"✅ Баланс пополнен на {amt}₽!")
                logger_bot.answer_callback_query(call.id, f"✅ {amt}₽ выдано")
            except:
                logger_bot.answer_callback_query(call.id, "❌ ошибка")
        logger_bot.edit_message_text("✅ готово", cid, mid, reply_markup=admin_kb())

    elif call.data.startswith("gd_"):
        pid = int(call.data.split("_")[1])
        row = dbf("SELECT user_id FROM pending_payments WHERE id=?", (pid,))
        if row:
            db("DELETE FROM pending_payments WHERE id=?", (pid,))
            try:
                worker_bot.send_message(row[0], "❌ Оплата не подтверждена. Обратитесь в поддержку.")
            except:
                pass
        logger_bot.answer_callback_query(call.id, "✅ отклонено")
        logger_bot.edit_message_text("❌ оплата отклонена", cid, mid, reply_markup=admin_kb())

    elif call.data == "a_spam":
        db("INSERT OR REPLACE INTO admin_sessions VALUES(?,?)", (ADMIN_ID, "spam"))
        logger_bot.delete_message(cid, mid)
        logger_bot.send_message(cid, "📢 отправь текст или фото для рассылки:")

    elif call.data == "a_promos":
        promos = dbfa("SELECT code, discount, uses_left FROM promo_codes WHERE is_active=1 AND uses_left>0")
        if not promos:
            text = "🎟 промокодов нет\n\nсоздать: /promo КОД СКИДКА ЛИМИТ"
        else:
            text = "🎟 промокоды:\n" + "\n".join(f"▫️ {c} — {d}% (осталось: {u})" for c, d, u in promos)
        logger_bot.edit_message_text(text, cid, mid, reply_markup=admin_kb())

@logger_bot.message_handler(commands=['promo'])
def promo_cmd(m):
    if str(m.from_user.id) != ADMIN_ID:
        return
    parts = m.text.split()
    if len(parts) != 4:
        logger_bot.reply_to(m, "❌ /promo КОД СКИДКА ЛИМИТ")
        return
    _, code, disc, lim = parts
    db("INSERT OR REPLACE INTO promo_codes VALUES(?,?,?,?,1)", (code.upper(), int(disc), int(lim), int(time.time())))
    logger_bot.reply_to(m, f"✅ {code.upper()} — {disc}%, {lim} раз")

@logger_bot.message_handler(func=lambda m: True, content_types=['text', 'photo'])
def spam_msg(m):
    if str(m.from_user.id) != ADMIN_ID:
        return
    row = dbf("SELECT step FROM admin_sessions WHERE user_id=?", (ADMIN_ID,))
    if not row or row[0] != "spam":
        return
    users = dbfa("SELECT user_id FROM users")
    sent = 0
    for (uid,) in users:
        try:
            if m.content_type == 'text':
                worker_bot.send_message(uid, m.text, parse_mode='HTML')
            else:
                photo = m.photo[-1].file_id
                worker_bot.send_photo(uid, photo, caption=m.caption or "", parse_mode='HTML')
            sent += 1
            time.sleep(0.05)
        except:
            pass
    logger_bot.reply_to(m, f"✅ рассылка: {sent} отправлено", reply_markup=admin_kb())
    db("DELETE FROM admin_sessions WHERE user_id=?", (ADMIN_ID,))

# ========== ЗАПУСК ==========
def run_bot(bot_instance, name):
    while True:
        try:
            print(f"✅ {name} запущен")
            bot_instance.polling(none_stop=True, interval=2, timeout=30)
        except Exception as e:
            print(f"❌ {name}: {e}")
            time.sleep(5)

if __name__ == "__main__":
    # Запускаем мониторинг зеркал
    threading.Thread(target=monitor_loop, daemon=True).start()
    
    # Запускаем бота-продаж и бота-логгера
    threading.Thread(target=run_bot, args=(worker_bot, "рабочий"), daemon=True).start()
    threading.Thread(target=run_bot, args=(logger_bot, "логгер"), daemon=True).start()
    
    print("✅ все боты запущены")
    
    while True:
        time.sleep(1)
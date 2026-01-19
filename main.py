import telebot
from telebot import types
import sqlite3
from datetime import datetime
import pytz
from apscheduler.schedulers.background import BackgroundScheduler

BOT_TOKEN = "8561775820:AAFXatDo0qSUVLaOpJ5wfWzkEI3o9f2Efbo"
bot = telebot.TeleBot(BOT_TOKEN)

# Московская временная зона
TZ = pytz.timezone("Europe/Moscow")

# -------------------- БАЗА ДАННЫХ --------------------
conn = sqlite3.connect("ka_system.db", check_same_thread=False)
cursor = conn.cursor()

# Пользователи
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT
)
""")

# Роли
cursor.execute("""
CREATE TABLE IF NOT EXISTS roles (
    user_id INTEGER,
    role TEXT
)
""")

# КА
cursor.execute("""
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    comment TEXT,
    creator_id INTEGER,
    executor_id INTEGER,
    deadline TEXT,
    status TEXT
)
""")

# Медиа проблемы
cursor.execute("""
CREATE TABLE IF NOT EXISTS task_media (
    task_id INTEGER,
    file_id TEXT,
    media_type TEXT
)
""")

# Отчёты
cursor.execute("""
CREATE TABLE IF NOT EXISTS reports (
    task_id INTEGER,
    file_id TEXT,
    media_type TEXT,
    created_at TEXT
)
""")

# История
cursor.execute("""
CREATE TABLE IF NOT EXISTS task_history (
    task_id INTEGER,
    action TEXT,
    user_id INTEGER,
    timestamp TEXT
)
""")

conn.commit()

# -------------------- ПЛАНИРОВЩИК --------------------
scheduler = BackgroundScheduler(timezone=TZ)
scheduler.start()

# -------------------- СОСТОЯНИЯ --------------------
states = {}

# -------------------- ВСПОМОГАТЕЛЬНО --------------------
def log_history(task_id, action, user_id):
    """Запись действий в историю"""
    cursor.execute("""
    INSERT INTO task_history VALUES (?, ?, ?, ?)
    """, (task_id, action, user_id, datetime.now(TZ).isoformat()))
    conn.commit()

# -------------------- START --------------------
@bot.message_handler(commands=["start"])
def start(message):
    cursor.execute("INSERT OR IGNORE INTO users VALUES (?, ?)",
                   (message.from_user.id, message.from_user.username))
    cursor.execute("INSERT OR IGNORE INTO roles VALUES (?, ?)",
                   (message.from_user.id, "user"))
    conn.commit()

    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("➕ Добавить КА", "📋 Мои КА", "📜 История")

    bot.send_message(
        message.chat.id,
        "👋 *Система контроля КА*\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=kb
    )

# -------------------- ДОБАВЛЕНИЕ КА --------------------
@bot.message_handler(func=lambda m: m.text == "➕ Добавить КА")
def add_ka(m):
    states[m.from_user.id] = {"step": "title"}
    bot.send_message(m.chat.id, "✏️ Введите название КА")

@bot.message_handler(func=lambda m: states.get(m.from_user.id, {}).get("step") == "title")
def ka_title(m):
    states[m.from_user.id]["title"] = m.text
    states[m.from_user.id]["step"] = "comment"
    bot.send_message(m.chat.id, "💬 Опишите проблему")

@bot.message_handler(func=lambda m: states.get(m.from_user.id, {}).get("step") == "comment")
def ka_comment(m):
    states[m.from_user.id]["comment"] = m.text
    states[m.from_user.id]["step"] = "media"
    bot.send_message(m.chat.id, "📸 Отправьте фото или видео")

@bot.message_handler(content_types=["photo", "video"])
def ka_media(m):
    if states.get(m.from_user.id, {}).get("step") != "media":
        return

    states[m.from_user.id]["media"] = {
        "file_id": m.photo[-1].file_id if m.content_type == "photo" else m.video.file_id,
        "type": m.content_type
    }

    kb = types.InlineKeyboardMarkup()
    cursor.execute("SELECT user_id, username FROM users WHERE user_id != ?", (m.from_user.id,))
    for uid, uname in cursor.fetchall():
        kb.add(types.InlineKeyboardButton(
            uname or str(uid),
            callback_data=f"exec_{uid}"
        ))

    states[m.from_user.id]["step"] = "executor"
    bot.send_message(m.chat.id, "👤 Выберите исполнителя", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("exec_"))
def ka_executor(c):
    states[c.from_user.id]["executor"] = int(c.data.split("_")[1])
    states[c.from_user.id]["step"] = "deadline"
    bot.send_message(c.message.chat.id, "⏰ Дедлайн: ДД.ММ.ГГГГ ЧЧ:ММ", parse_mode="Markdown")

@bot.message_handler(func=lambda m: states.get(m.from_user.id, {}).get("step") == "deadline")
def ka_deadline(m):
    deadline = TZ.localize(datetime.strptime(m.text, "%d.%m.%Y %H:%M"))
    st = states.pop(m.from_user.id)

    cursor.execute("""
    INSERT INTO tasks VALUES (NULL,?,?,?,?,?,?)
    """, (st["title"], st["comment"], m.from_user.id, st["executor"], deadline.isoformat(), "open"))
    task_id = cursor.lastrowid

    cursor.execute("INSERT INTO task_media VALUES (?,?,?)",
                   (task_id, st["media"]["file_id"], st["media"]["type"]))
    conn.commit()

    log_history(task_id, "Создано КА", m.from_user.id)
    schedule_reminder(task_id)

    bot.send_message(m.chat.id, "✅ КА создано")

# -------------------- МОИ КА --------------------
@bot.message_handler(func=lambda m: m.text == "📋 Мои КА")
def my_tasks(m):
    cursor.execute("""
    SELECT id, title, status FROM tasks
    WHERE executor_id=?
    """, (m.from_user.id,))
    kb = types.InlineKeyboardMarkup()
    for i, t, s in cursor.fetchall():
        kb.add(types.InlineKeyboardButton(f"{t} [{s}]", callback_data=f"task_{i}"))
    bot.send_message(m.chat.id, "📋 Ваши задачи:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("task_"))
def open_task(c):
    task_id = int(c.data.split("_")[1])

    cursor.execute("""
    SELECT title, comment, deadline, status FROM tasks WHERE id=?
    """, (task_id,))
    t, com, d, s = cursor.fetchone()

    kb = types.InlineKeyboardMarkup()
    if s != "closed":
        kb.add(types.InlineKeyboardButton("📤 Отправить отчёт", callback_data=f"report_{task_id}"))

    bot.send_message(
        c.message.chat.id,
        f"📌 *{t}*\n\n💬 {com}\n⏰ {d}\n📍 Статус: {s}",
        parse_mode="Markdown",
        reply_markup=kb
    )

# -------------------- ОТЧЁТ --------------------
@bot.callback_query_handler(func=lambda c: c.data.startswith("report_"))
def report_start(c):
    states[c.from_user.id] = {"step": "report", "task": int(c.data.split("_")[1])}
    bot.send_message(c.message.chat.id, "📸 Отправьте фото/видео отчёта")

@bot.message_handler(content_types=["photo", "video"])
def report_save(m):
    if states.get(m.from_user.id, {}).get("step") != "report":
        return

    task_id = states.pop(m.from_user.id)["task"]

    file_id = m.photo[-1].file_id if m.content_type == "photo" else m.video.file_id

    cursor.execute("INSERT INTO reports VALUES (?,?,?,?)",
                   (task_id, file_id, m.content_type, datetime.now(TZ).isoformat()))
    cursor.execute("UPDATE tasks SET status='on_review' WHERE id=?", (task_id,))
    conn.commit()

    log_history(task_id, "Отправлен отчёт", m.from_user.id)

    bot.send_message(m.chat.id, "✅ Отчёт отправлен")

# -------------------- ИСТОРИЯ --------------------
@bot.message_handler(func=lambda m: m.text == "📜 История")
def history(m):
    cursor.execute("""
    SELECT t.title, h.action, h.timestamp
    FROM task_history h
    JOIN tasks t ON t.id=h.task_id
    WHERE h.user_id=?
    ORDER BY h.timestamp DESC
    LIMIT 10
    """, (m.from_user.id,))

    text = "📜 *Последние действия:*\n\n"
    for t, a, ts in cursor.fetchall():
        text += f"• {t} — {a}\n🕒 {ts}\n\n"

    bot.send_message(m.chat.id, text, parse_mode="Markdown")

# -------------------- НАПОМИНАНИЯ --------------------
def schedule_reminder(task_id):
    def notify():
        cursor.execute("""
        SELECT executor_id, deadline, title FROM tasks
        WHERE id=? AND status!='closed'
        """, (task_id,))
        row = cursor.fetchone()
        if not row:
            return

        uid, d, t = row
        left = datetime.fromisoformat(d) - datetime.now(TZ)
        if left.total_seconds() > 0:
            bot.send_message(uid, f"⏳ Напоминание\nКА: {t}\nОсталось: {left}")

    scheduler.add_job(notify, "interval", hours=6, id=str(task_id), replace_existing=True)

# -------------------- ЗАПУСК --------------------
print("🚀 Бот запущен")
bot.infinity_polling()
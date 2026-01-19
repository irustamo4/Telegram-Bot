"""
Telegram-бот для контроля корректирующих действий (КА)
"""

# ==================== ИМПОРТЫ ====================
import telebot
from telebot import types
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
import threading
import time
from pytz import timezone
import os

# ==================== НАСТРОЙКА ЛОГИРОВАНИЯ ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ==================== КОНСТАНТЫ И НАСТРОЙКИ ====================
API_TOKEN = "8561775820:AAFXatDo0qSUVLaOpJ5wfWzkEI3o9f2Efbo"
MOSCOW_TZ = timezone("Europe/Moscow")
REMINDER_INTERVAL = 6 * 3600  # 6 часов в секундах
DATABASE_NAME = "corrective_actions_v4.db"

# ==================== ИНИЦИАЛИЗАЦИЯ БОТА ====================
bot = telebot.TeleBot(API_TOKEN, parse_mode="HTML")

# ==================== ХРАНЕНИЕ СОСТОЯНИЙ (FSM АНАЛОГ) ====================
# Для хранения состояний пользователей
user_states = {}
user_data = {}


# ==================== КЛАСС ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ ====================
class Database:
    def __init__(self, db_name: str = DATABASE_NAME):
        self.db_name = db_name
        self.init_db()

    def init_db(self):
        """Инициализация таблиц в базе данных"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()

            # Таблица пользователей системы
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS system_users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT NOT NULL,
                    role TEXT DEFAULT 'user',  -- admin, manager, user
                    chat_id INTEGER,  -- ID личного чата с ботом
                    registered_from_chat_id INTEGER,  -- Из какого чата зарегистрирован
                    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Таблица групповых чатов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS group_chats (
                    chat_id INTEGER PRIMARY KEY,
                    chat_title TEXT NOT NULL,
                    admin_id INTEGER NOT NULL,  -- Тот, кто добавил бота
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Таблица корректирующих действий
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS corrective_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    creator_id INTEGER NOT NULL,
                    creator_name TEXT NOT NULL,
                    assignee_id INTEGER NOT NULL,
                    assignee_name TEXT NOT NULL,
                    photo_id TEXT,
                    video_id TEXT,
                    description TEXT NOT NULL,
                    deadline TIMESTAMP NOT NULL,
                    status TEXT DEFAULT 'active',  -- active, completed, expired
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    last_reminder TIMESTAMP
                )
            ''')

            # Индексы для ускорения поиска
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_ca_assignee ON corrective_actions(assignee_id, status)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_ca_creator ON corrective_actions(creator_id, status)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_ca_deadline ON corrective_actions(deadline, status)
            ''')

            conn.commit()
            logger.info("База данных инициализирована")

    # ==================== ПОЛЬЗОВАТЕЛИ ====================
    def register_user(self, user_id: int, username: str, full_name: str,
                      role: str = 'user', chat_id: Optional[int] = None,
                      from_chat_id: Optional[int] = None):
        """Регистрация/обновление пользователя в системе"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO system_users 
                (user_id, username, full_name, role, chat_id, registered_from_chat_id)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, username, full_name, role, chat_id, from_chat_id))
            conn.commit()
            logger.info(f"Пользователь {full_name} ({user_id}) зарегистрирован как {role}")

    def get_user(self, user_id: int) -> Optional[Dict]:
        """Получение информации о пользователе"""
        with sqlite3.connect(self.db_name) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM system_users WHERE user_id = ?",
                (user_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_user_role(self, user_id: int) -> str:
        """Получение роли пользователя"""
        user = self.get_user(user_id)
        return user['role'] if user else 'user'

    def can_create_tasks(self, user_id: int) -> bool:
        """Может ли пользователь создавать задачи"""
        role = self.get_user_role(user_id)
        return role in ['admin', 'manager']

    def is_admin(self, user_id: int) -> bool:
        """Является ли пользователь администратором"""
        return self.get_user_role(user_id) == 'admin'

    def promote_to_manager(self, admin_id: int, user_id: int) -> bool:
        """Повышение пользователя до менеджера (только админ)"""
        # Проверяем, что повышает админ
        if not self.is_admin(admin_id):
            return False

        # Проверяем, что пользователь существует
        user = self.get_user(user_id)
        if not user:
            return False

        # Нельзя повысить администратора
        if user['role'] == 'admin':
            return False

        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE system_users SET role = 'manager' WHERE user_id = ?",
                (user_id,)
            )
            conn.commit()
            logger.info(f"Пользователь {user_id} повышен до менеджера админом {admin_id}")
            return True

    def get_all_users(self, exclude_user_id: Optional[int] = None) -> List[Dict]:
        """Получение всех пользователей системы (кроме указанного)"""
        with sqlite3.connect(self.db_name) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if exclude_user_id:
                cursor.execute(
                    "SELECT * FROM system_users WHERE user_id != ? AND role != 'admin' ORDER BY full_name",
                    (exclude_user_id,)
                )
            else:
                cursor.execute(
                    "SELECT * FROM system_users WHERE role != 'admin' ORDER BY full_name"
                )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_regular_users(self) -> List[Dict]:
        """Получение списка обычных пользователей"""
        with sqlite3.connect(self.db_name) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM system_users WHERE role = 'user' ORDER BY full_name"
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_managers(self) -> List[Dict]:
        """Получение списка менеджеров"""
        with sqlite3.connect(self.db_name) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM system_users WHERE role IN ('admin', 'manager') ORDER BY role DESC"
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    # ==================== ГРУППОВЫЕ ЧАТЫ ====================
    def register_group_chat(self, chat_id: int, chat_title: str, admin_id: int):
        """Регистрация группового чата"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO group_chats (chat_id, chat_title, admin_id)
                VALUES (?, ?, ?)
            ''', (chat_id, chat_title, admin_id))
            conn.commit()
            logger.info(f"Групповой чат {chat_title} зарегистрирован, админ: {admin_id}")

    def get_group_chat(self, chat_id: int) -> Optional[Dict]:
        """Получение информации о групповом чате"""
        with sqlite3.connect(self.db_name) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM group_chats WHERE chat_id = ?",
                (chat_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_group_admin(self, chat_id: int) -> Optional[int]:
        """Получение ID администратора группового чата"""
        chat = self.get_group_chat(chat_id)
        return chat['admin_id'] if chat else None

    # ==================== КОРРЕКТИРУЮЩИЕ ДЕЙСТВИЯ ====================
    def add_ca(self,
               creator_id: int,
               creator_name: str,
               assignee_id: int,
               assignee_name: str,
               photo_id: Optional[str],
               video_id: Optional[str],
               description: str,
               deadline: datetime) -> int:
        """Добавление нового корректирующего действия"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO corrective_actions 
                (creator_id, creator_name, assignee_id, assignee_name, 
                 photo_id, video_id, description, deadline)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (creator_id, creator_name, assignee_id, assignee_name,
                  photo_id, video_id, description, deadline))
            conn.commit()
            ca_id = cursor.lastrowid
            logger.info(f"Добавлено КА #{ca_id} от {creator_name} для {assignee_name}")
            return ca_id

    def get_user_tasks(self, user_id: int, is_creator: bool = False) -> List[Dict]:
        """Получение задач пользователя"""
        with sqlite3.connect(self.db_name) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if is_creator:
                cursor.execute('''
                    SELECT * FROM corrective_actions 
                    WHERE creator_id = ? AND status = 'active'
                    ORDER BY deadline
                ''', (user_id,))
            else:
                cursor.execute('''
                    SELECT * FROM corrective_actions 
                    WHERE assignee_id = ? AND status = 'active'
                    ORDER BY deadline
                ''', (user_id,))

            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_ca_by_id(self, ca_id: int) -> Optional[Dict]:
        """Получение КА по ID"""
        with sqlite3.connect(self.db_name) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM corrective_actions WHERE id = ?",
                (ca_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def mark_as_completed(self, ca_id: int):
        """Отметка КА как выполненного"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE corrective_actions 
                SET status = 'completed', completed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (ca_id,))
            conn.commit()
            logger.info(f"КА #{ca_id} отмечен как выполненный")

    def update_last_reminder(self, ca_id: int):
        """Обновление времени последнего напоминания"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE corrective_actions 
                SET last_reminder = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (ca_id,))
            conn.commit()

    def get_active_tasks_for_reminders(self) -> List[Dict]:
        """Получение активных задач для напоминаний"""
        with sqlite3.connect(self.db_name) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM corrective_actions 
                WHERE status = 'active' 
                AND (last_reminder IS NULL OR 
                     datetime(last_reminder) < datetime('now', '-6 hours'))
                ORDER BY deadline
            ''')
            rows = cursor.fetchall()
            return [dict(row) for row in rows]


# ==================== ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ====================
db = Database()


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def format_task_details(task: Dict, for_assignee: bool = False) -> str:
    """Форматирование деталей задачи"""
    deadline = datetime.fromisoformat(task['deadline'])
    created_at = datetime.fromisoformat(task['created_at'])
    now = datetime.now(MOSCOW_TZ)

    # Рассчитываем оставшееся время
    time_left = deadline - now
    days = time_left.days
    hours = time_left.seconds // 3600

    # Эмодзи для срочности
    if days < 0:
        urgency_emoji = "🚨🚨🚨"
        time_text = f"ПРОСРОЧЕНО на {-days} дн."
    elif days == 0:
        urgency_emoji = "⚠️"
        time_text = f"СЕГОДНЯ до {deadline.strftime('%H:%M')}"
    elif days == 1:
        urgency_emoji = "⚠️"
        time_text = f"ЗАВТРА до {deadline.strftime('%H:%M')}"
    elif days < 3:
        urgency_emoji = "⏳"
        time_text = f"{days} дн. {hours} ч."
    else:
        urgency_emoji = "📅"
        time_text = f"{deadline.strftime('%d.%m.%Y %H:%M')}"

    # Формируем текст
    if for_assignee:
        title = f"📋 <b>Задача #{task['id']}</b>\n"
    else:
        title = f"👤 <b>Задача для: {task['assignee_name']}</b>\n"

    text = f"""
{title}
{urgency_emoji} <b>Срок:</b> {time_text}

📝 <b>Описание:</b>
{task['description']}

👤 <b>Создал:</b> {task['creator_name']}
📅 <b>Создано:</b> {created_at.strftime('%d.%m.%Y %H:%M')}

🆔 <b>ID задачи:</b> #{task['id']}
"""

    if task['photo_id']:
        text += "\n📸 <b>Прикреплено фото</b>"
    if task['video_id']:
        text += "\n🎥 <b>Прикреплено видео</b>"

    return text


# ==================== КЛАВИАТУРЫ ====================
def get_main_keyboard(user_role: str = 'user') -> types.ReplyKeyboardMarkup:
    """Основная клавиатура для личных сообщений"""
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)

    if user_role in ['admin', 'manager']:
        keyboard.add(types.KeyboardButton("➕ Создать задачу"))

    keyboard.add(
        types.KeyboardButton("📋 Мои задачи (исполнитель)"),
        types.KeyboardButton("👁 Мои задачи (создатель)")
    )

    if user_role == 'admin':
        keyboard.add(types.KeyboardButton("👑 Управление менеджерами"))

    keyboard.add(types.KeyboardButton("ℹ️ Помощь"))
    return keyboard


def get_group_keyboard() -> types.ReplyKeyboardMarkup:
    """Клавиатура для группового чата"""
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    keyboard.add(
        types.KeyboardButton("👥 Зарегистрировать участников"),
        types.KeyboardButton("📊 Статистика базы"),
        types.KeyboardButton("ℹ️ Помощь")
    )
    return keyboard


def get_cancel_keyboard() -> types.ReplyKeyboardMarkup:
    """Клавиатура для отмены"""
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(types.KeyboardButton("❌ Отмена"))
    return keyboard


def get_admin_keyboard() -> types.InlineKeyboardMarkup:
    """Клавиатура для администратора"""
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton("📈 Повысить до менеджера", callback_data="promote_manager"),
        types.InlineKeyboardButton("👥 Список менеджеров", callback_data="list_managers"),
        types.InlineKeyboardButton("📋 Список пользователей", callback_data="list_users"),
        types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
    )
    return keyboard


def get_users_for_promotion_keyboard(users: List[Dict]) -> types.InlineKeyboardMarkup:
    """Клавиатура с пользователями для повышения"""
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    for user in users:
        button_text = f"👤 {user['full_name']}"
        if user['username']:
            button_text += f" (@{user['username']})"

        keyboard.add(types.InlineKeyboardButton(
            button_text,
            callback_data=f"promote_{user['user_id']}"
        ))

    keyboard.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_admin"))
    return keyboard


def get_assignee_keyboard(users: List[Dict]) -> types.InlineKeyboardMarkup:
    """Клавиатура для выбора ответственного"""
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    for user in users:
        button_text = f"👤 {user['full_name']}"
        if user['username']:
            button_text += f" (@{user['username']})"

        keyboard.add(types.InlineKeyboardButton(
            button_text,
            callback_data=f"assign_{user['user_id']}_{user['full_name']}"
        ))

    keyboard.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_assignment"))
    return keyboard


# ==================== ОСНОВНЫЕ ОБРАБОТЧИКИ ====================
@bot.message_handler(commands=['start'])
def cmd_start(message: types.Message):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    username = message.from_user.username
    full_name = message.from_user.full_name

    # Регистрируем пользователя
    db.register_user(user_id, username, full_name, 'user', message.chat.id)

    # Получаем роль пользователя
    user_role = db.get_user_role(user_id)

    if message.chat.type == "private":
        # Личные сообщения
        welcome_text = f"""
👋 <b>Добро пожаловать, {full_name}!</b>

Ваша роль в системе: <b>{'Администратор' if user_role == 'admin' else 'Менеджер' if user_role == 'manager' else 'Исполнитель'}</b>

<b>Доступные функции:</b>
"""

        if user_role in ['admin', 'manager']:
            welcome_text += "• Создавать и назначать задачи\n"

        welcome_text += """• Просматривать назначенные вам задачи
• Отмечать выполнение задач
• Получать напоминания о сроках
"""

        if user_role == 'admin':
            welcome_text += "• Управлять менеджерами системы\n"

        bot.send_message(
            message.chat.id,
            welcome_text,
            reply_markup=get_main_keyboard(user_role)
        )
    else:
        # Групповой чат - ТОЛЬКО для регистрации пользователей
        try:
            # Проверяем, является ли пользователь администратором чата
            chat_admins = bot.get_chat_administrators(message.chat.id)
            is_admin = False

            for admin in chat_admins:
                if admin.user.id == user_id and admin.status in ['creator', 'administrator']:
                    is_admin = True
                    break

            if is_admin:
                # Регистрируем чат
                db.register_group_chat(
                    message.chat.id,
                    message.chat.title or "Групповой чат",
                    user_id
                )

                # Повышаем пользователя до админа системы
                db.register_user(user_id, username, full_name, 'admin', message.chat.id, message.chat.id)
                user_role = 'admin'

                group_welcome = f"""
👑 <b>Вы назначены администратором системы!</b>

Чат "{message.chat.title}" зарегистрирован в системе.

<b>Этот чат используется ТОЛЬКО для регистрации пользователей в базу данных.</b>

Все задачи создаются и назначаются в <b>личных сообщениях</b> с ботом.

<b>Команды в этом чате:</b>
• Используйте кнопку "👥 Зарегистрировать участников" для добавления всех в базу
• "📊 Статистика базы" - просмотр зарегистрированных пользователей

Для работы с задачами перейдите в личные сообщения с ботом.
"""
            else:
                group_welcome = f"""
👋 <b>Привет, {full_name}!</b>

Этот чат используется ТОЛЬКО для регистрации пользователей в базу данных.

<b>Ваша роль:</b> {'Менеджер' if user_role == 'manager' else 'Исполнитель'}

Все задачи создаются и назначаются в <b>личных сообщениях</b> с ботом.

Для работы перейдите в личные сообщения с ботом.
"""

            bot.send_message(
                message.chat.id,
                group_welcome,
                reply_markup=get_group_keyboard()
            )

        except Exception as e:
            logger.error(f"Ошибка при регистрации чата: {e}")
            bot.send_message(
                message.chat.id,
                "❌ Ошибка инициализации бота в чате. Убедитесь, что бот имеет права администратора."
            )


@bot.message_handler(
    func=lambda message: message.chat.type != "private" and message.text == "👥 Зарегистрировать участников")
def register_all_members(message: types.Message):
    """Регистрация всех участников чата в базу данных"""
    try:
        chat_id = message.chat.id

        # Проверяем права (только администраторы чата)
        chat_admins = bot.get_chat_administrators(chat_id)
        is_admin = False

        for admin in chat_admins:
            if admin.user.id == message.from_user.id and admin.status in ['creator', 'administrator']:
                is_admin = True
                break

        if not is_admin:
            bot.send_message(chat_id, "❌ Только администраторы чата могут регистрировать участников.")
            return

        # Получаем список участников чата
        chat_members_count = bot.get_chat_member_count(chat_id)

        # Создаем клавиатуру для подтверждения
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            types.InlineKeyboardButton("✅ Да, зарегистрировать", callback_data=f"register_confirm_{chat_id}"),
            types.InlineKeyboardButton("❌ Нет, отмена", callback_data="register_cancel")
        )

        bot.send_message(
            chat_id,
            f"📝 <b>Регистрация участников чата</b>\n\n"
            f"В чате обнаружено: <b>{chat_members_count}</b> участников\n\n"
            f"<b>Внимание:</b> Будут зарегистрированы все участники чата (кроме ботов).\n"
            f"Это необходимо для возможности назначать им задачи.\n\n"
            f"<i>Продолжить регистрацию?</i>",
            reply_markup=keyboard
        )

    except Exception as e:
        logger.error(f"Ошибка при регистрации участников: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при получении списка участников.")


@bot.callback_query_handler(func=lambda call: call.data.startswith('register_confirm_'))
def confirm_registration(call: types.CallbackQuery):
    """Подтверждение регистрации участников"""
    chat_id = int(call.data.split('_')[2])

    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="🔄 <b>Регистрация участников...</b>\n\nПожалуйста, подождите..."
        )

        # Получаем администраторов чата
        chat_admins = bot.get_chat_administrators(chat_id)
        registered_count = 0

        # Регистрируем администраторов чата
        for admin in chat_admins:
            user = admin.user
            if not user.is_bot:
                role = 'manager' if admin.status in ['creator', 'administrator'] else 'user'
                db.register_user(
                    user.id,
                    user.username,
                    user.full_name,
                    role,
                    None,  # chat_id будет установлен при личном обращении
                    chat_id
                )
                registered_count += 1

        # Пытаемся получить других участников (ограничение API)
        try:
            # Регистрируем отправителя команды, если он не администратор
            sender = call.from_user
            if not any(admin.user.id == sender.id for admin in chat_admins):
                db.register_user(
                    sender.id,
                    sender.username,
                    sender.full_name,
                    'user',
                    None,
                    chat_id
                )
                registered_count += 1
        except:
            pass

        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"✅ <b>Регистрация завершена!</b>\n\n"
                 f"Зарегистрировано участников: <b>{registered_count}</b>\n\n"
                 f"Теперь эти пользователи могут получать задачи.\n"
                 f"Для создания задач перейдите в личные сообщения с ботом."
        )

    except Exception as e:
        logger.error(f"Ошибка при регистрации: {e}")
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="❌ Ошибка при регистрации участников."
        )


@bot.callback_query_handler(func=lambda call: call.data == "register_cancel")
def cancel_registration(call: types.CallbackQuery):
    """Отмена регистрации"""
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="❌ Регистрация участников отменена."
    )


@bot.message_handler(func=lambda message: message.chat.type != "private" and message.text == "📊 Статистика базы")
def show_database_stats(message: types.Message):
    """Показать статистику базы данных"""
    try:
        # Получаем общее количество пользователей
        all_users = db.get_all_users()
        managers = db.get_managers()
        regular_users = db.get_regular_users()

        stats_text = f"""
📊 <b>Статистика базы данных</b>

👥 <b>Всего пользователей:</b> {len(all_users)}
👑 <b>Администраторов/менеджеров:</b> {len(managers)}
👤 <b>Обычных пользователей:</b> {len(regular_users)}

<b>Список зарегистрированных пользователей:</b>
"""

        # Добавляем список пользователей (первые 20)
        for i, user in enumerate(all_users[:20], 1):
            role_emoji = "👑" if user['role'] == 'admin' else "📋" if user['role'] == 'manager' else "👤"
            username = f" (@{user['username']})" if user['username'] else ""
            stats_text += f"\n{i}. {role_emoji} {user['full_name']}{username} - {user['role']}"

        if len(all_users) > 20:
            stats_text += f"\n\n<i>... и еще {len(all_users) - 20} пользователей</i>"

        stats_text += "\n\n<b>Для создания задач перейдите в личные сообщения с ботом.</b>"

        bot.send_message(message.chat.id, stats_text)

    except Exception as e:
        logger.error(f"Ошибка при получении статистики: {e}")
        bot.send_message(message.chat.id, "❌ Ошибка при получении статистики.")


# ==================== ЛИЧНЫЕ СООБЩЕНИЯ: СОЗДАНИЕ ЗАДАЧИ ====================
@bot.message_handler(func=lambda message: message.chat.type == "private" and
                                          message.text in ["➕ Создать задаче", "➕ Создать задачу"])
def start_private_task(message: types.Message):
    """Начало создания задачи в личных сообщениях"""
    user_id = message.from_user.id
    can_create = db.can_create_tasks(user_id)

    if not can_create:
        bot.send_message(message.chat.id, "❌ У вас нет прав для создания задач.")
        return

    # Проверяем, есть ли зарегистрированные пользователи
    all_users = db.get_all_users(exclude_user_id=user_id)

    if not all_users:
        bot.send_message(
            message.chat.id,
            "❌ В базе данных нет других пользователей.\n\n"
            "Для создания задач необходимо сначала зарегистрировать пользователей.\n"
            "1. Добавьте бота в групповой чат с сотрудниками\n"
            "2. Дайте боту права администратора\n"
            "3. В групповом чате используйте кнопку '👥 Зарегистрировать участников'\n"
            "4. Затем вернитесь сюда и создайте задачу"
        )
        return

    # Устанавливаем состояние пользователя
    user_states[user_id] = {
        'state': 'waiting_for_assignee',
        'step': 1,
        'creator_name': message.from_user.full_name
    }

    bot.send_message(
        message.chat.id,
        "👥 <b>ШАГ 1 из 4: Выберите ответственного</b>\n\n"
        "Кому назначить задачу?",
        reply_markup=get_assignee_keyboard(all_users)
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith('assign_'))
def process_private_assignee(call: types.CallbackQuery):
    """Обработка выбора ответственного в личных сообщениях"""
    user_id = call.from_user.id

    if user_id not in user_states or user_states[user_id]['state'] != 'waiting_for_assignee':
        bot.answer_callback_query(call.id, "❌ Время ожидания истекло")
        return

    # Извлекаем данные из callback_data: assign_{user_id}_{full_name}
    parts = call.data.split('_')
    if len(parts) < 3:
        bot.answer_callback_query(call.id, "❌ Ошибка выбора пользователя")
        return

    assignee_id = int(parts[1])
    assignee_name = '_'.join(parts[2:])  # Восстанавливаем имя (может содержать _)

    # Обновляем состояние
    user_states[user_id]['state'] = 'waiting_for_photo'
    user_states[user_id]['assignee_id'] = assignee_id
    user_states[user_id]['assignee_name'] = assignee_name

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"✅ Ответственный: {assignee_name}\n\n"
             f"📸 <b>ШАГ 2 из 4: Прикрепите фото или видео проблемы</b>\n\n"
             f"Сделайте четкое фото или видео несоответствия.\n"
             f"<i>Или отправьте 'пропустить' чтобы продолжить без медиа.</i>",
        reply_markup=None
    )

    bot.answer_callback_query(call.id)


@bot.message_handler(content_types=['photo', 'video', 'text'],
                     func=lambda message: message.chat.type == 'private' and
                                          message.from_user.id in user_states and
                                          user_states[message.from_user.id].get('state') == 'waiting_for_photo')
def process_private_photo(message: types.Message):
    """Обработка фото/видео в личных сообщениях"""
    user_id = message.from_user.id
    user_state = user_states[user_id]

    photo_id = None
    video_id = None

    if message.text and message.text.lower() == "пропустить":
        media_type = "без медиа"
    elif message.photo:
        photo_id = message.photo[-1].file_id
        media_type = "фото"
    elif message.video:
        video_id = message.video.file_id
        media_type = "видео"
    else:
        bot.send_message(message.chat.id, "❌ Пожалуйста, прикрепите фото, видео или напишите 'пропустить'.")
        return

    # Сохраняем данные
    user_state['photo_id'] = photo_id
    user_state['video_id'] = video_id
    user_state['state'] = 'waiting_for_description'

    bot.send_message(
        message.chat.id,
        f"✅ {media_type.capitalize()} принято!\n\n"
        f"📝 <b>ШАГ 3 из 4: Опишите проблему</b>\n\n"
        f"Кратко и понятно опишите:\n"
        f"• Что произошло?\n"
        f"• Где обнаружено?\n"
        f"• Почему это проблема?\n\n"
        f"<i>Пример: 'На линии №3 обнаружена течь масла из-под клапана ХК-12.'</i>"
    )


@bot.message_handler(func=lambda message: message.chat.type == 'private' and
                                          message.from_user.id in user_states and
                                          user_states[message.from_user.id].get('state') == 'waiting_for_description')
def process_private_description(message: types.Message):
    """Обработка описания в личных сообщениях"""
    user_id = message.from_user.id
    user_state = user_states[user_id]

    if not message.text or len(message.text.strip()) < 5:
        bot.send_message(message.chat.id, "❌ Описание должно содержать минимум 5 символов.")
        return

    description = message.text.strip()
    user_state['description'] = description
    user_state['state'] = 'waiting_for_deadline'

    bot.send_message(
        message.chat.id,
        f"✅ Описание сохранено!\n\n"
        f"📅 <b>ШАГ 4 из 4: Укажите срок выполнения</b>\n\n"
        f"Введите дату и время в формате:\n"
        f"<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n\n"
        f"<i>Пример: 25.12.2024 18:00</i>\n\n"
        f"Или укажите количество часов от текущего момента:\n"
        f"<code>+часы</code>\n\n"
        f"<i>Пример: +24 (срок через 24 часа)</i>"
    )


@bot.message_handler(func=lambda message: message.chat.type == 'private' and
                                          message.from_user.id in user_states and
                                          user_states[message.from_user.id].get('state') == 'waiting_for_deadline')
def process_private_deadline(message: types.Message):
    """Обработка срока выполнения в личных сообщениях"""
    user_id = message.from_user.id
    user_state = user_states[user_id]
    deadline_input = message.text.strip()

    try:
        now = datetime.now(MOSCOW_TZ)

        # Обработка формата "+часы"
        if deadline_input.startswith('+'):
            hours = int(deadline_input[1:])
            if hours <= 0:
                raise ValueError("Время должно быть положительным")
            deadline = now + timedelta(hours=hours)
        else:
            # Обработка формата "ДД.ММ.ГГГГ ЧЧ:ММ"
            deadline = datetime.strptime(deadline_input, "%d.%m.%Y %H:%M")
            deadline = MOSCOW_TZ.localize(deadline)

            if deadline <= now:
                bot.send_message(message.chat.id, "❌ Срок должен быть в будущем!")
                return

        # Создаем задачу в БД
        ca_id = db.add_ca(
            creator_id=user_id,
            creator_name=user_state['creator_name'],
            assignee_id=user_state['assignee_id'],
            assignee_name=user_state['assignee_name'],
            photo_id=user_state.get('photo_id'),
            video_id=user_state.get('video_id'),
            description=user_state['description'],
            deadline=deadline
        )

        # Уведомляем создателя
        creator_text = f"""
✅ <b>Задача создана!</b>

🆔 <b>Задача #{ca_id}</b>
👤 <b>Ответственный:</b> {user_state['assignee_name']}
📅 <b>Срок:</b> {deadline.strftime('%d.%m.%Y %H:%M')}

📝 <b>Описание:</b>
{user_state['description']}

<i>Исполнитель получил уведомление о новой задаче.</i>
"""

        # Отправляем фото/видео если есть
        try:
            if user_state.get('photo_id'):
                bot.send_photo(
                    message.chat.id,
                    photo=user_state['photo_id'],
                    caption=creator_text,
                    reply_markup=get_main_keyboard(db.get_user_role(user_id))
                )
            elif user_state.get('video_id'):
                bot.send_video(
                    message.chat.id,
                    video=user_state['video_id'],
                    caption=creator_text,
                    reply_markup=get_main_keyboard(db.get_user_role(user_id))
                )
            else:
                bot.send_message(
                    message.chat.id,
                    creator_text,
                    reply_markup=get_main_keyboard(db.get_user_role(user_id))
                )
        except Exception as e:
            logger.error(f"Ошибка отправки медиа создателю: {e}")
            bot.send_message(
                message.chat.id,
                creator_text,
                reply_markup=get_main_keyboard(db.get_user_role(user_id))
            )

        # Уведомляем исполнителя
        assignee_info = db.get_user(user_state['assignee_id'])
        if assignee_info and assignee_info.get('chat_id'):
            try:
                assignee_text = f"""
🎯 <b>Вам назначена новая задача!</b>

🆔 <b>Задача #{ca_id}</b>
👤 <b>От:</b> {user_state['creator_name']}
📅 <b>Срок:</b> {deadline.strftime('%d.%m.%Y %H:%M')}
⏰ <b>До дедлайна:</b> {int((deadline - now).total_seconds() / 3600)} ч.

📝 <b>Описание:</b>
{user_state['description']}

<i>Напоминания будут приходить каждые 6 часов до выполнения задачи.</i>
"""

                if user_state.get('photo_id'):
                    bot.send_photo(
                        assignee_info['chat_id'],
                        photo=user_state['photo_id'],
                        caption=assignee_text
                    )
                elif user_state.get('video_id'):
                    bot.send_video(
                        assignee_info['chat_id'],
                        video=user_state['video_id'],
                        caption=assignee_text
                    )
                else:
                    bot.send_message(
                        assignee_info['chat_id'],
                        assignee_text
                    )

            except Exception as e:
                logger.error(f"Не удалось уведомить исполнителя: {e}")
                bot.send_message(
                    message.chat.id,
                    f"⚠️ <b>Внимание:</b> Не удалось отправить уведомление исполнителю.\n"
                    f"Возможно, пользователь {user_state['assignee_name']} еще не начал диалог с ботом.",
                    reply_markup=get_main_keyboard(db.get_user_role(user_id))
                )
        else:
            bot.send_message(
                message.chat.id,
                f"⚠️ <b>Внимание:</b> Исполнитель {user_state['assignee_name']} не имеет личного чата с ботом.\n"
                f"Попросите его написать боту в личные сообщения команду /start",
                reply_markup=get_main_keyboard(db.get_user_role(user_id))
            )

        # Удаляем состояние пользователя
        del user_states[user_id]

    except ValueError as e:
        error_msg = str(e)
        if "time data" in error_msg:
            bot.send_message(
                message.chat.id,
                "❌ Неверный формат даты!\n\n"
                "Введите в формате: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n"
                "Или: <code>+часы</code>\n\n"
                "<i>Примеры: 25.12.2024 18:00 или +24</i>"
            )
        else:
            bot.send_message(message.chat.id, f"❌ Ошибка: {error_msg}")


# ==================== ЛИЧНЫЕ СООБЩЕНИЯ: ПРОСМОТР ЗАДАЧ ====================
@bot.message_handler(
    func=lambda message: message.text == "📋 Мои задачи (исполнитель)" and message.chat.type == "private")
def show_assignee_tasks(message: types.Message):
    """Показать задачи, где пользователь - исполнитель"""
    user_id = message.from_user.id
    tasks = db.get_user_tasks(user_id, is_creator=False)

    if not tasks:
        bot.send_message(
            message.chat.id,
            "📭 У вас нет активных задач.\n"
            "Когда вам назначат задачу, она появится здесь.",
            reply_markup=get_main_keyboard(db.get_user_role(user_id))
        )
        return

    text = f"📋 <b>Ваши активные задачи:</b> ({len(tasks)})\n\n"

    for task in tasks[:10]:  # Ограничим 10 задачами
        deadline = datetime.fromisoformat(task['deadline'])
        now = datetime.now(MOSCOW_TZ)
        days_left = (deadline - now).days

        status_emoji = "🚨" if days_left < 0 else "⚠️" if days_left == 0 else "📅"

        text += f"{status_emoji} <b>Задача #{task['id']}</b>\n"
        text += f"📝 {task['description'][:50]}...\n"
        text += f"👤 От: {task['creator_name']}\n"
        text += f"📅 До: {deadline.strftime('%d.%m.%Y %H:%M')}\n"

        if days_left < 0:
            text += f"⏰ <b>Просрочено на {-days_left} дн.</b>\n"
        elif days_left == 0:
            text += f"⏰ <b>Срок сегодня!</b>\n"
        else:
            text += f"⏰ Осталось: {days_left} дн.\n"

        text += "━━━━━━━━━━━━━━━━━━\n"

    if len(tasks) > 10:
        text += f"\n<i>И еще {len(tasks) - 10} задач...</i>"

    bot.send_message(message.chat.id, text)


@bot.message_handler(func=lambda message: message.text == "👁 Мои задачи (создатель)" and message.chat.type == "private")
def show_creator_tasks(message: types.Message):
    """Показать задачи, где пользователь - создатель"""
    user_id = message.from_user.id
    tasks = db.get_user_tasks(user_id, is_creator=True)

    if not tasks:
        bot.send_message(
            message.chat.id,
            "📭 Вы еще не создавали задач.\n"
            "Создайте первую задачу с помощью кнопки '➕ Создать задачу'.",
            reply_markup=get_main_keyboard(db.get_user_role(user_id))
        )
        return

    text = f"👁 <b>Созданные вами задачи:</b> ({len(tasks)})\n\n"

    completed = 0
    for task in tasks[:10]:
        deadline = datetime.fromisoformat(task['deadline'])
        status = task['status']

        if status == 'completed':
            status_emoji = "✅"
            completed += 1
        elif status == 'active':
            days_left = (deadline - datetime.now(MOSCOW_TZ)).days
            status_emoji = "🚨" if days_left < 0 else "⚠️" if days_left == 0 else "📅"
        else:
            status_emoji = "❓"

        text += f"{status_emoji} <b>Задача #{task['id']}</b>\n"
        text += f"👤 Исполнитель: {task['assignee_name']}\n"
        text += f"📅 Срок: {deadline.strftime('%d.%m.%Y %H:%M')}\n"
        text += f"📊 Статус: {'Выполнено' if status == 'completed' else 'Активно'}\n"
        text += "━━━━━━━━━━━━━━━━━━\n"

    text += f"\n📈 <b>Статистика:</b>\n"
    text += f"• Всего: {len(tasks)} задач\n"
    text += f"• Выполнено: {completed} задач\n"
    text += f"• Активно: {len(tasks) - completed} задач"

    bot.send_message(message.chat.id, text)


# ==================== АДМИНИСТРИРОВАНИЕ ====================
@bot.message_handler(func=lambda message: message.text == "👑 Управление менеджерами" and message.chat.type == "private")
def admin_panel(message: types.Message):
    """Панель администратора"""
    user_id = message.from_user.id
    if not db.is_admin(user_id):
        bot.send_message(message.chat.id, "❌ Эта функция доступна только администраторам.")
        return

    bot.send_message(
        message.chat.id,
        "👑 <b>Панель администратора</b>\n\n"
        "Вы можете управлять правами доступа других пользователей.",
        reply_markup=get_admin_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data == "promote_manager")
def promote_manager_menu(call: types.CallbackQuery):
    """Меню для повышения пользователей"""
    user_id = call.from_user.id
    if not db.is_admin(user_id):
        bot.answer_callback_query(call.id, "❌ Доступ запрещен")
        return

    # Получаем список обычных пользователей
    users = db.get_regular_users()

    if not users:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="📭 Нет пользователей для повышения.\n"
                 "Все пользователи уже имеют роли администратора или менеджера.",
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_admin")
            )
        )
        return

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="👥 <b>Выберите пользователя для повышения до менеджера:</b>\n\n"
             "Менеджеры могут создавать и назначать задачи.",
        reply_markup=get_users_for_promotion_keyboard(users)
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith('promote_'))
def promote_user(call: types.CallbackQuery):
    """Повышение пользователя до менеджера"""
    admin_id = call.from_user.id
    user_id = int(call.data.split('_')[1])

    if not db.is_admin(admin_id):
        bot.answer_callback_query(call.id, "❌ Доступ запрещен")
        return

    # Проверяем, не пытаемся ли повысить себя
    if user_id == admin_id:
        bot.answer_callback_query(call.id, "❌ Вы не можете изменить свою собственную роль")
        return

    # Повышаем пользователя
    success = db.promote_to_manager(admin_id, user_id)

    if success:
        # Получаем информацию о пользователе
        user = db.get_user(user_id)

        # Уведомляем администратора
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"✅ <b>Пользователь повышен!</b>\n\n"
                 f"👤 {user['full_name']}\n"
                 f"📧 @{user['username'] if user['username'] else 'нет username'}\n"
                 f"🎯 Новая роль: <b>Менеджер</b>\n\n"
                 f"Теперь этот пользователь может создавать и назначать задачи.",
            reply_markup=types.InlineKeyboardMarkup(row_width=1).add(
                types.InlineKeyboardButton("👥 Еще пользователи", callback_data="promote_manager"),
                types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_admin")
            )
        )

        # Пытаемся уведомить пользователя
        if user.get('chat_id'):
            try:
                bot.send_message(
                    user['chat_id'],
                    f"🎉 <b>Поздравляем!</b>\n\n"
                    f"Вас повысили до <b>Менеджера</b> в системе контроля КА.\n\n"
                    f"Теперь вы можете:\n"
                    f"• Создавать корректирующие действия\n"
                    f"• Назначать задачи сотрудникам\n"
                    f"• Использовать кнопку '➕ Создать задачу'\n\n"
                    f"<i>Обновите меню бота командой /start</i>"
                )
            except Exception as e:
                logger.error(f"Не удалось уведомить пользователя {user_id}: {e}")

    else:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="❌ Не удалось повысить пользователя.\n"
                 "Возможные причины:\n"
                 "• Пользователь не найден\n"
                 "• Пользователь уже администратор\n"
                 "• Ошибка системы",
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("🔙 Назад", callback_data="promote_manager")
            )
        )


# ==================== ОБРАБОТЧИКИ КНОПОК И ОТМЕНЫ ====================
@bot.callback_query_handler(func=lambda call: call.data == "back_to_main")
def back_to_main_menu(call: types.CallbackQuery):
    """Возврат в главное меню"""
    user_role = db.get_user_role(call.from_user.id)
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="Главное меню:",
        reply_markup=get_main_keyboard(user_role)
    )


@bot.callback_query_handler(func=lambda call: call.data == "back_to_admin")
def back_to_admin_panel(call: types.CallbackQuery):
    """Возврат в панель администратора"""
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="👑 <b>Панель администратора</b>",
        reply_markup=get_admin_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data == "cancel_assignment")
def cancel_assignment(call: types.CallbackQuery):
    """Отмена назначения задачи"""
    user_id = call.from_user.id
    if user_id in user_states:
        del user_states[user_id]

    user_role = db.get_user_role(user_id)
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="❌ Создание задачи отменено."
    )

    # Отправляем новое сообщение с главным меню
    bot.send_message(
        call.message.chat.id,
        "Главное меню:",
        reply_markup=get_main_keyboard(user_role)
    )

    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda message: message.text == "❌ Отмена")
def cancel_action(message: types.Message):
    """Отмена текущего действия"""
    user_id = message.from_user.id
    if user_id in user_states:
        del user_states[user_id]

    user_role = db.get_user_role(user_id)

    if message.chat.type == "private":
        bot.send_message(
            message.chat.id,
            "❌ Действие отменено.",
            reply_markup=get_main_keyboard(user_role)
        )
    else:
        bot.send_message(
            message.chat.id,
            "❌ Действие отменено.",
            reply_markup=get_group_keyboard()
        )


@bot.message_handler(func=lambda message: message.text == "ℹ️ Помощь")
def show_help(message: types.Message):
    """Показать справку"""
    if message.chat.type == "private":
        help_text = """
<b>📚 Руководство по использованию системы:</b>

<b>Групповой чат (только регистрация):</b>
• Добавьте бота в групповой чат с сотрудниками
• Дайте боту права администратора
• Используйте кнопку "👥 Зарегистрировать участников"
• Теперь этих пользователей можно назначать на задачи

<b>Личные сообщения (вся работа):</b>
• Создавайте задачи с фото/видео
• Назначайте ответственных из зарегистрированных
• Указывайте сроки выполнения
• Просматривайте свои задачи
• Получайте напоминания

<b>Роли в системе:</b>
• <b>Администратор</b> - тот, кто добавил бота в чат
• <b>Менеджер</b> - может создавать и назначать задачи
• <b>Исполнитель</b> - выполняет назначенные задачи

<b>Напоминания:</b>
• Автоматически каждые 6 часов в личные сообщения
• Уведомления о просроченных задачах
• Напоминания о скором дедлайне
"""
    else:
        help_text = """
<b>📚 Этот чат используется ТОЛЬКО для регистрации пользователей!</b>

<b>Что делать здесь:</b>
1. Убедитесь, что бот - администратор чата
2. Нажмите "👥 Зарегистрировать участников"
3. Подтвердите регистрацию
4. Все участники будут добавлены в базу данных

<b>Что делать дальше:</b>
1. Каждый участник должен начать личный диалог с ботом (команда /start)
2. Администраторы/менеджеры создают задачи в личных сообщениях
3. Исполнители получают задачи и напоминания в личных сообщениях

<b>Для работы с задачами перейдите в личные сообщения с ботом!</b>
"""

    bot.send_message(message.chat.id, help_text)


# ==================== СИСТЕМА НАПОМИНАНИЙ ====================
def reminder_system():
    """Фоновая задача для отправки напоминаний"""
    while True:
        try:
            tasks = db.get_active_tasks_for_reminders()

            for task in tasks:
                deadline = datetime.fromisoformat(task['deadline'])
                now = datetime.now(MOSCOW_TZ)
                assignee_id = task['assignee_id']

                # Получаем chat_id исполнителя
                assignee = db.get_user(assignee_id)
                if not assignee or not assignee.get('chat_id'):
                    continue

                chat_id = assignee['chat_id']

                # Формируем текст напоминания
                days_left = (deadline - now).days

                if days_left < 0:
                    # Просроченная задача
                    reminder_text = f"""
🚨🚨🚨 <b>ЗАДАЧА ПРОСРОЧЕНА!</b>

🆔 <b>Задача #{task['id']}</b>
📝 {task['description'][:100]}...
👤 От: {task['creator_name']}
📅 Срок истёк: {deadline.strftime('%d.%m.%Y %H:%M')}
⏰ Просрочка: {-days_left} дн.

❗️ <b>Немедленно примите меры!</b>
"""
                elif days_left == 0:
                    # Срок сегодня
                    hours_left = int((deadline - now).total_seconds() / 3600)
                    reminder_text = f"""
⚠️ <b>СРОК ВЫПОЛНЕНИЯ СЕГОДНЯ!</b>

🆔 <b>Задача #{task['id']}</b>
📝 {task['description'][:100]}...
👤 От: {task['creator_name']}
📅 Срок: {deadline.strftime('%d.%m.%Y %H:%M')}
⏰ Осталось: {hours_left} ч.

<b>Не забудьте выполнить задачу!</b>
"""
                elif days_left <= 2:
                    # Скоро срок
                    reminder_text = f"""
⏰ <b>Напоминание о задаче</b>

🆔 <b>Задача #{task['id']}</b>
📝 {task['description'][:100]}...
👤 От: {task['creator_name']}
📅 Срок: {deadline.strftime('%d.%m.%Y %H:%M')}
⏰ Осталось: {days_left} дн.

Не забудьте выполнить задачу в срок!
"""
                else:
                    # Обычное напоминание (каждые 6 часов)
                    reminder_text = f"""
🔔 <b>Напоминание о задаче</b>

🆔 <b>Задача #{task['id']}</b>
📝 {task['description'][:100]}...
👤 От: {task['creator_name']}
📅 Срок: {deadline.strftime('%d.%m.%Y %H:%M')}
⏰ Осталось: {days_left} дн.

Статус задачи: Активен
"""

                # Отправляем напоминание
                try:
                    bot.send_message(
                        chat_id,
                        reminder_text
                    )

                    # Обновляем время последнего напоминания
                    db.update_last_reminder(task['id'])

                    logger.info(f"Отправлено напоминание для задачи #{task['id']}")

                except Exception as e:
                    logger.error(f"Не удалось отправить напоминание: {e}")

            # Ждем 10 минут перед следующей проверкой
            time.sleep(600)

        except Exception as e:
            logger.error(f"Ошибка в системе напоминаний: {e}")
            time.sleep(60)


# ==================== ЗАПУСК БОТА ====================
def start_bot():
    """Запуск бота и системы напоминаний"""
    logger.info("Запуск бота...")

    # Запускаем систему напоминаний в отдельном потоке
    reminder_thread = threading.Thread(target=reminder_system, daemon=True)
    reminder_thread.start()
    logger.info("Система напоминаний запущена")

    # Запускаем бота
    try:
        bot.polling(none_stop=True, interval=0, timeout=20)
    except Exception as e:
        logger.error(f"Ошибка запуска бота: {e}")
        time.sleep(5)
        start_bot()  # Перезапуск при ошибке


if __name__ == "__main__":
    start_bot()
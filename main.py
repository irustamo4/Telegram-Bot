import logging
import sqlite3
from datetime import datetime

import telebot
from telebot import types

# ==================== НАСТРОЙКА ЛОГИРОВАНИЯ ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ==================== КОНСТАНТЫ ====================
API_TOKEN = "8561775820:AAFXatDo0qSUVLaOpJ5wfWzkEI3o9f2Efbo"  # Замените на ваш токен от @BotFather
DATABASE_NAME = "non_conformities.db"

# ==================== ИНИЦИАЛИЗАЦИЯ БОТА ====================
bot = telebot.TeleBot(API_TOKEN, parse_mode="HTML")


# ==================== БАЗА ДАННЫХ ====================
class Database:
    def __init__(self, db_name=DATABASE_NAME):
        self.db_name = db_name
        self.init_db()

    def init_db(self):
        """Инициализация базы данных"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()

            # Таблица несоответствий
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS non_conformities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    user_name TEXT NOT NULL,
                    photo_id TEXT,
                    video_id TEXT,
                    description TEXT NOT NULL,
                    location TEXT NOT NULL,
                    nctype TEXT NOT NULL,  -- тип несоответствия
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'new'  -- new, in_progress, fixed
                )
            ''')

            # Таблица пользователей
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT NOT NULL,
                    department TEXT,
                    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Таблица отделов/цехов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS departments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE
                )
            ''')

            # Предзаполняем список цехов
            departments = [
                "Цех 1 - Подготовка сырья",
                "Цех 2 - Основное производство",
                "Цех 3 - Фасовка и упаковка",
                "Склад сырья",
                "Склад готовой продукции",
                "Лаборатория контроля качества"
            ]

            for dept in departments:
                try:
                    cursor.execute("INSERT OR IGNORE INTO departments (name) VALUES (?)", (dept,))
                except:
                    pass

            conn.commit()
            logger.info("База данных инициализирована")

    def add_non_conformity(self, user_id, user_name, photo_id, video_id, description, location, nctype):
        """Добавление нового несоответствия"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO non_conformities 
                (user_id, user_name, photo_id, video_id, description, location, nctype)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, user_name, photo_id, video_id, description, location, nctype))
            conn.commit()
            return cursor.lastrowid

    def register_user(self, user_id, username, full_name):
        """Регистрация пользователя"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO users (user_id, username, full_name)
                VALUES (?, ?, ?)
            ''', (user_id, username, full_name))
            conn.commit()

    def get_user_stats(self, user_id):
        """Получение статистики пользователя"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()

            # Общее количество зафиксированных несоответствий
            cursor.execute("SELECT COUNT(*) FROM non_conformities WHERE user_id = ?", (user_id,))
            total = cursor.fetchone()[0]

            # За сегодня
            cursor.execute("""
                SELECT COUNT(*) FROM non_conformities 
                WHERE user_id = ? AND DATE(created_at) = DATE('now')
            """, (user_id,))
            today = cursor.fetchone()[0]

            # По типам
            cursor.execute("""
                SELECT nctype, COUNT(*) as count 
                FROM non_conformities 
                WHERE user_id = ? 
                GROUP BY nctype
            """, (user_id,))
            by_type = cursor.fetchall()

            return {
                'total': total,
                'today': today,
                'by_type': dict(by_type) if by_type else {}
            }

    def get_recent_non_conformities(self, user_id, limit=5):
        """Получение последних несоответствий пользователя"""
        with sqlite3.connect(self.db_name) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM non_conformities 
                WHERE user_id = ? 
                ORDER BY created_at DESC 
                LIMIT ?
            ''', (user_id, limit))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_departments(self):
        """Получение списка отделов/цехов"""
        with sqlite3.connect(self.db_name) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM departments ORDER BY name")
            return [row[0] for row in cursor.fetchall()]

    def get_daily_report(self):
        """Отчет за сегодня"""
        with sqlite3.connect(self.db_name) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT 
                    nctype,
                    location,
                    COUNT(*) as count,
                    GROUP_CONCAT(user_name, ', ') as reporters
                FROM non_conformities 
                WHERE DATE(created_at) = DATE('now')
                GROUP BY nctype, location
                ORDER BY count DESC
            ''')
            rows = cursor.fetchall()
            return [dict(row) for row in rows]


# ==================== ИНИЦИАЛИЗАЦИЯ БАЗЫ ====================
db = Database()

# ==================== ХРАНЕНИЕ СОСТОЯНИЙ ====================
# Для отслеживания текущего действия пользователя
user_states = {}

# Типы несоответствий
NON_CONFORMITY_TYPES = {
    "сырье": "Сырье и материалы",
    "процесс": "Технологический процесс",
    "упаковка": "Упаковка и маркировка",
    "оборудование": "Оборудование",
    "персонал": "Персонал и обучение",
    "другое": "Другое"
}


# ==================== КЛАВИАТУРЫ ====================
def get_main_keyboard() -> types.ReplyKeyboardMarkup:
    """Главное меню"""
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        types.KeyboardButton("📝 Зафиксировать проблему"),
        types.KeyboardButton("📊 Моя статистика"),
        types.KeyboardButton("📋 Последние записи"),
        types.KeyboardButton("📅 Отчет за сегодня"),
        types.KeyboardButton("ℹ️ Помощь")
    )
    return keyboard


def get_cancel_keyboard() -> types.ReplyKeyboardMarkup:
    """Клавиатура для отмены"""
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(types.KeyboardButton("❌ Отмена"))
    return keyboard


def get_nctype_keyboard() -> types.InlineKeyboardMarkup:
    """Клавиатура для выбора типа несоответствия"""
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    for key, value in NON_CONFORMITY_TYPES.items():
        keyboard.add(types.InlineKeyboardButton(value, callback_data=f"nctype_{key}"))
    keyboard.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel"))
    return keyboard


def get_departments_keyboard() -> types.InlineKeyboardMarkup:
    """Клавиатура для выбора цеха/отдела"""
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    departments = db.get_departments()
    for dept in departments:
        keyboard.add(types.InlineKeyboardButton(dept, callback_data=f"dept_{dept}"))
    keyboard.add(types.InlineKeyboardButton("📍 Другое место", callback_data="other_location"))
    keyboard.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel"))
    return keyboard


# ==================== ОБРАБОТЧИКИ КОМАНД ====================
@bot.message_handler(commands=['start'])
def start_command(message):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    username = message.from_user.username
    full_name = message.from_user.full_name

    # Регистрируем пользователя
    db.register_user(user_id, username, full_name)

    welcome_text = f"""
👋 <b>Добро пожаловать, {full_name}!</b>

Я - ваш мобильный журнал несоответствий на пищевом производстве.

<b>Что я умею:</b>
📝 <b>Зафиксировать проблему</b> - быстро записать несоответствие с фото/видео
📊 <b>Статистика</b> - ваши отчеты по найденным проблемам
📋 <b>Последние записи</b> - история ваших фиксаций
📅 <b>Отчет за сегодня</b> - сводка по всем несоответствиям за день

<b>Как работать:</b>
1. Нажмите "Зафиксировать проблему"
2. Сфотографируйте проблему
3. Опишите что не так
4. Укажите тип и место
5. Готово! Запись сохранена.

<b>Важно:</b> Каждая запись помогает улучшать качество продукции!
"""

    bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_keyboard())


@bot.message_handler(commands=['help'])
def help_command(message):
    """Обработчик команды /help"""
    help_text = """
<b>📚 Справка по использованию бота:</b>

<b>Основные функции:</b>
• <b>Зафиксировать проблему</b> - создать новую запись о несоответствии
• <b>Моя статистика</b> - посмотреть ваши отчеты
• <b>Последние записи</b> - история ваших фиксаций
• <b>Отчет за сегодня</b> - сводка за текущий день

<b>Процесс фиксации проблемы:</b>
1. Нажмите "Зафиксировать проблему"
2. Прикрепите фото или видео проблемы (можно пропустить)
3. Подробно опишите проблему
4. Выберите тип несоответствия:
   • <b>Сырье и материалы</b> - проблемы с исходным сырьем
   • <b>Технологический процесс</b> - нарушения в процессе производства
   • <b>Упаковка и маркировка</b> - дефекты упаковки, ошибки в маркировке
   • <b>Оборудование</b> - неисправности оборудования
   • <b>Персонал и обучение</b> - нарушения персоналом
   • <b>Другое</b> - прочие несоответствия
5. Выберите место обнаружения

<b>Все данные сохраняются в базу для последующего анализа.</b>
"""
    bot.send_message(message.chat.id, help_text)


@bot.message_handler(func=lambda message: message.text == "📝 Зафиксировать проблему")
def start_reporting(message):
    """Начало процесса фиксации несоответствия"""
    user_id = message.from_user.id
    user_states[user_id] = {
        'state': 'waiting_photo',
        'step': 1,
        'user_name': message.from_user.full_name
    }

    instruction = """
📸 <b>ШАГ 1 из 4: Сфотографируйте проблему</b>

Прикрепите фото или видео несоответствия.
Это поможет лучше понять проблему.

<i>Если фото/видео нет, отправьте текст "пропустить"</i>
"""

    bot.send_message(message.chat.id, instruction, reply_markup=get_cancel_keyboard())


@bot.message_handler(content_types=['photo', 'video', 'text'])
def handle_media(message):
    """Обработка фото/видео или пропуска"""
    user_id = message.from_user.id

    if user_id not in user_states:
        return

    state = user_states[user_id]['state']

    if state == 'waiting_photo':
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

        user_states[user_id]['photo_id'] = photo_id
        user_states[user_id]['video_id'] = video_id
        user_states[user_id]['state'] = 'waiting_description'

        bot.send_message(
            message.chat.id,
            f"✅ {media_type.capitalize()} принято!\n\n"
            f"📝 <b>ШАГ 2 из 4: Опишите проблему</b>\n\n"
            f"Подробно опишите несоответствие:\n"
            f"• Что именно не так?\n"
            f"• Когда обнаружено?\n"
            f"• Какие могут быть последствия?\n\n"
            f"<i>Пример: 'На линии фасовки №3 обнаружена течь масла из-под уплотнительной манжеты. "
            f"Масло попадает на упаковку продукции.'</i>"
        )

    elif state == 'waiting_description':
        if not message.text or len(message.text.strip()) < 10:
            bot.send_message(message.chat.id, "❌ Описание должно содержать минимум 10 символов.")
            return

        user_states[user_id]['description'] = message.text.strip()
        user_states[user_id]['state'] = 'waiting_nctype'

        bot.send_message(
            message.chat.id,
            "✅ Описание сохранено!\n\n"
            "🏷️ <b>ШАГ 3 из 4: Выберите тип несоответствия</b>\n\n"
            "К какой категории относится проблема?",
            reply_markup=get_nctype_keyboard()
        )

    elif state == 'waiting_location_text':
        if not message.text or len(message.text.strip()) < 3:
            bot.send_message(message.chat.id, "❌ Укажите место обнаружения.")
            return

        user_states[user_id]['location'] = message.text.strip()

        # Сохраняем запись
        save_non_conformity(user_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('nctype_'))
def handle_nctype(call):
    """Обработка выбора типа несоответствия"""
    user_id = call.from_user.id

    if user_id not in user_states or user_states[user_id]['state'] != 'waiting_nctype':
        bot.answer_callback_query(call.id, "❌ Время ожидания истекло")
        return

    nctype_key = call.data.split('_')[1]
    nctype_name = NON_CONFORMITY_TYPES.get(nctype_key, "Другое")

    user_states[user_id]['nctype'] = nctype_key
    user_states[user_id]['nctype_name'] = nctype_name
    user_states[user_id]['state'] = 'waiting_location'

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=f"✅ Тип: {nctype_name}\n\n"
             f"📍 <b>ШАГ 4 из 4: Где обнаружена проблема?</b>\n\n"
             f"Выберите цех/отдел из списка:",
        reply_markup=get_departments_keyboard()
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('dept_'))
def handle_department(call):
    """Обработка выбора отдела"""
    user_id = call.from_user.id

    if user_id not in user_states or user_states[user_id]['state'] != 'waiting_location':
        bot.answer_callback_query(call.id, "❌ Время ожидания истекло")
        return

    location = call.data.split('_', 1)[1]
    user_states[user_id]['location'] = location

    # Сохраняем запись
    save_non_conformity(user_id)

    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == 'other_location')
def handle_other_location(call):
    """Запрос другого места"""
    user_id = call.from_user.id

    if user_id not in user_states or user_states[user_id]['state'] != 'waiting_location':
        bot.answer_callback_query(call.id, "❌ Время ожидания истекло")
        return

    user_states[user_id]['state'] = 'waiting_location_text'

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="📍 <b>Укажите место обнаружения:</b>\n\n"
             "Напишите название цеха, линии или другого места:"
    )
    bot.answer_callback_query(call.id)


def save_non_conformity(user_id):
    """Сохранение несоответствия в базу"""
    try:
        state = user_states[user_id]

        record_id = db.add_non_conformity(
            user_id=user_id,
            user_name=state['user_name'],
            photo_id=state.get('photo_id'),
            video_id=state.get('video_id'),
            description=state['description'],
            location=state['location'],
            nctype=state['nctype']
        )

        # Формируем сообщение об успехе
        success_text = f"""
✅ <b>Несоответствие зафиксировано!</b>

🆔 <b>Номер записи:</b> #{record_id}
👤 <b>Сотрудник:</b> {state['user_name']}
🏷️ <b>Тип:</b> {state['nctype_name']}
📍 <b>Место:</b> {state['location']}
📅 <b>Время:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}

📝 <b>Описание:</b>
{state['description']}

<i>Запись сохранена в журнале несоответствий.</i>
"""

        # Отправляем сообщение
        chat_id = bot.get_chat(user_id).id

        try:
            if state.get('photo_id'):
                bot.send_photo(chat_id, state['photo_id'], caption=success_text, reply_markup=get_main_keyboard())
            elif state.get('video_id'):
                bot.send_video(chat_id, state['video_id'], caption=success_text, reply_markup=get_main_keyboard())
            else:
                bot.send_message(chat_id, success_text, reply_markup=get_main_keyboard())
        except:
            bot.send_message(chat_id, success_text, reply_markup=get_main_keyboard())

        # Удаляем состояние пользователя
        del user_states[user_id]

        logger.info(f"Сохранено несоответствие #{record_id} от пользователя {user_id}")

    except Exception as e:
        logger.error(f"Ошибка сохранения несоответствия: {e}")
        bot.send_message(user_id, "❌ Ошибка сохранения записи. Попробуйте снова.", reply_markup=get_main_keyboard())


@bot.message_handler(func=lambda message: message.text == "📊 Моя статистика")
def show_stats(message):
    """Показать статистику пользователя"""
    user_id = message.from_user.id
    stats = db.get_user_stats(user_id)

    if stats['total'] == 0:
        bot.send_message(message.chat.id, "📭 Вы еще не фиксировали несоответствий.")
        return

    # Формируем текст статистики
    stats_text = f"""
📊 <b>Ваша статистика</b>

<b>Всего зафиксировано:</b> {stats['total']} несоответствий
<b>Сегодня:</b> {stats['today']} несоответствий

<b>Распределение по типам:</b>
"""

    for nctype_key, count in stats['by_type'].items():
        nctype_name = NON_CONFORMITY_TYPES.get(nctype_key, nctype_key)
        stats_text += f"• {nctype_name}: {count}\n"

    # Процент от общего
    if stats['total'] > 0:
        today_percent = (stats['today'] / stats['total']) * 100
        stats_text += f"\n<b>Сегодняшние записи:</b> {today_percent:.1f}% от общего числа"

    bot.send_message(message.chat.id, stats_text)


@bot.message_handler(func=lambda message: message.text == "📋 Последние записи")
def show_recent(message):
    """Показать последние записи пользователя"""
    user_id = message.from_user.id
    records = db.get_recent_non_conformities(user_id, limit=5)

    if not records:
        bot.send_message(message.chat.id, "📭 У вас пока нет записей.")
        return

    records_text = f"""
📋 <b>Ваши последние записи</b> ({len(records)})

"""

    for i, record in enumerate(records, 1):
        nctype_name = NON_CONFORMITY_TYPES.get(record['nctype'], record['nctype'])
        created_at = datetime.strptime(record['created_at'], '%Y-%m-%d %H:%M:%S').strftime('%d.%m.%Y %H:%M')

        records_text += f"""
{i}. <b>Запись #{record['id']}</b>
   🏷️ Тип: {nctype_name}
   📍 Место: {record['location']}
   📅 Дата: {created_at}
   📝 {record['description'][:50]}...
   ━━━━━━━━━━━━━━━━━━
"""

    bot.send_message(message.chat.id, records_text)


@bot.message_handler(func=lambda message: message.text == "📅 Отчет за сегодня")
def daily_report(message):
    """Показать отчет за сегодня"""
    report = db.get_daily_report()

    if not report:
        bot.send_message(message.chat.id, "📅 Сегодня несоответствий не зафиксировано.")
        return

    report_text = """
📅 <b>Отчет за сегодня</b>

"""

    total = 0
    for item in report:
        total += item['count']

    report_text += f"<b>Всего несоответствий:</b> {total}\n\n"

    for item in report:
        nctype_name = NON_CONFORMITY_TYPES.get(item['nctype'], item['nctype'])
        report_text += f"""
🏷️ <b>{nctype_name}</b>
📍 Место: {item['location']}
📊 Количество: {item['count']}
👤 Сотрудники: {item['reporters'][:50]}...
━━━━━━━━━━━━━━━━━━
"""

    # Анализ
    report_text += "\n<b>📈 Анализ:</b>\n"

    if len(report) > 0:
        most_common = max(report, key=lambda x: x['count'])
        nctype_name = NON_CONFORMITY_TYPES.get(most_common['nctype'], most_common['nctype'])
        report_text += f"• Наиболее частый тип: {nctype_name} ({most_common['count']} случаев)\n"
        report_text += f"• Проблемное место: {most_common['location']}\n"

    bot.send_message(message.chat.id, report_text)


@bot.message_handler(func=lambda message: message.text == "❌ Отмена")
def cancel_action(message):
    """Отмена текущего действия"""
    user_id = message.from_user.id

    if user_id in user_states:
        del user_states[user_id]

    bot.send_message(
        message.chat.id,
        "❌ Действие отменено.",
        reply_markup=get_main_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data == 'cancel')
def cancel_callback(call):
    """Отмена через callback"""
    user_id = call.from_user.id

    if user_id in user_states:
        del user_states[user_id]

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="❌ Действие отменено."
    )

    bot.send_message(
        call.message.chat.id,
        "Главное меню:",
        reply_markup=get_main_keyboard()
    )

    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda message: message.text == "ℹ️ Помощь")
def show_help(message):
    """Показать справку"""
    help_text = """
<b>📋 Мобильный журнал несоответствий</b>

<b>Назначение:</b>
Этот бот предназначен для быстрой фиксации несоответствий на пищевом производстве.

<b>Как использовать:</b>
1. <b>Зафиксировать проблему</b> - основная функция
2. <b>Моя статистика</b> - ваши результаты
3. <b>Последние записи</b> - история фиксаций
4. <b>Отчет за сегодня</b> - общая сводка

<b>Типы несоответствий:</b>
• <b>Сырье и материалы</b> - проблемы с качеством сырья
• <b>Технологический процесс</b> - нарушения технологии
• <b>Упаковка и маркировка</b> - дефекты упаковки
• <b>Оборудование</b> - неисправности техники
• <b>Персонал и обучение</b> - ошибки сотрудников
• <b>Другое</b> - прочие проблемы

<b>Данные используются для:</b>
• Анализа причин несоответствий
• Улучшения процессов производства
• Обучения персонала
• Повышения качества продукции

<b>Каждая запись важна для улучшения качества!</b>
"""
    bot.send_message(message.chat.id, help_text)


# ==================== ЗАПУСК БОТА ====================
def start_bot():
    """Запуск бота"""
    logger.info("Запуск бота-регистратора несоответствий...")

    try:
        bot.polling(none_stop=True, interval=0, timeout=20)
    except Exception as e:
        logger.error(f"Ошибка запуска бота: {e}")
        import time
        time.sleep(5)
        start_bot()  # Перезапуск при ошибке


if __name__ == "__main__":
    start_bot()

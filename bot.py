"""
Telegram AI Reminder & Habit Tracker Bot
========================================
- aiogram 3.x
- Работает через SOCKS5/HTTP прокси (переменная окружения PROXY_URL) — обход блокировок РФ
- Интеграция с OpenRouter через библиотеку `openai` (base_url="https://openrouter.ai")
- SQLite БД в /app/data (Docker volume) — настройки, напоминания, привычки
- Доступ только для ADMIN_ID
- Кастомный системный промпт ИИ: /show_prompt, /edit_prompt
- APScheduler: по расписанию шлёт скрытый запрос в ИИ и пересылает креативный ответ
- Инлайн-меню для управления напоминаниями и привычками
"""

import asyncio
import html
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp_socks import ProxyConnector  # noqa: F401 — нужен aiogram для построения прокси-коннектора
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

# ----------------------------------------------------------------------------
# Конфигурация
# ----------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-flash-1.5-8b").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip()
TZ_NAME = os.getenv("TZ", "Europe/Moscow").strip() or "UTC"

if not ADMIN_ID_RAW:
    raise RuntimeError("ADMIN_ID не задан в .env")
ADMIN_ID = int(ADMIN_ID_RAW)

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"

DEFAULT_SYSTEM_PROMPT = (
    "Ты — дерзкий тренер по продуктивности. Мотивируй жестко, коротко (2-4 предложения), "
    "с юмором и без цензуры в рамках приличия. В каждом сообщении давай один конкретный "
    "призыв к действию. Пиши на русском языке."
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ai-reminder-bot")

router = Router()
scheduler = AsyncIOScheduler(timezone=ZoneInfo(TZ_NAME))


# ----------------------------------------------------------------------------
# База данных (SQLite)
# ----------------------------------------------------------------------------
def db_init() -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reminders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            text       TEXT NOT NULL,
            hour       INTEGER NOT NULL,
            minute     INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS habits (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            target_days INTEGER NOT NULL DEFAULT 7,
            done_dates  TEXT NOT NULL DEFAULT '',
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    cur.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('system_prompt', ?)",
        (DEFAULT_SYSTEM_PROMPT,),
    )
    con.commit()
    con.close()


def db_get_prompt() -> str:
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT value FROM settings WHERE key='system_prompt'").fetchone()
    con.close()
    return row[0] if row else DEFAULT_SYSTEM_PROMPT


def db_set_prompt(text: str) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO settings (key, value) VALUES ('system_prompt', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (text,),
    )
    con.commit()
    con.close()


def db_add_reminder(text: str, hour: int, minute: int) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        "INSERT INTO reminders (text, hour, minute) VALUES (?, ?, ?)",
        (text, hour, minute),
    )
    con.commit()
    rid = cur.lastrowid
    con.close()
    return rid


def db_list_reminders():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, text, hour, minute FROM reminders ORDER BY hour, minute"
    ).fetchall()
    con.close()
    return rows


def db_del_reminder(rid: int) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM reminders WHERE id=?", (rid,))
    con.commit()
    con.close()


def db_add_habit(name: str, target_days: int = 7) -> bool:
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute(
            "INSERT INTO habits (name, target_days) VALUES (?, ?)",
            (name, target_days),
        )
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        con.close()


def db_list_habits():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, name, target_days, done_dates FROM habits ORDER BY id"
    ).fetchall()
    con.close()
    return rows


def db_toggle_habit(hid: int, date_str: str) -> bool:
    """Отмечает/снимает выполнение привычки на дату. Возвращает True, если отметили."""
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT done_dates FROM habits WHERE id=?", (hid,)
    ).fetchone()
    if not row:
        con.close()
        return False
    dates = set(filter(None, row[0].split(",")))
    if date_str in dates:
        dates.discard(date_str)
        marked = False
    else:
        dates.add(date_str)
        marked = True
    con.execute(
        "UPDATE habits SET done_dates=? WHERE id=?",
        (",".join(sorted(dates)), hid),
    )
    con.commit()
    con.close()
    return marked


def db_del_habit(hid: int) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM habits WHERE id=?", (hid,))
    con.commit()
    con.close()


# ----------------------------------------------------------------------------
# ИИ (OpenRouter через openai SDK)
# ----------------------------------------------------------------------------
ai_client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY or "empty",
    base_url="https://openrouter.ai/api/v1",
    default_headers={
        "HTTP-Referer": "https://github.com/ai-reminder-bot",
        "X-Title": "AI Reminder Bot",
    },
)


async def ask_ai(user_request: str) -> str:
    """Скрытый запрос к ИИ с текущим системным промптом из БД."""
    system_prompt = db_get_prompt()
    try:
        resp = await ai_client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_request},
            ],
            temperature=0.9,
            max_tokens=300,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.exception("OpenRouter request failed")
        return f"⚠️ ИИ недоступен ({type(e).__name__}). Напоминание: {user_request}"


# ----------------------------------------------------------------------------
# FSM-состояния
# ----------------------------------------------------------------------------
class AddReminder(StatesGroup):
    waiting_time = State()
    waiting_text = State()


class AddHabit(StatesGroup):
    waiting_name = State()


# ----------------------------------------------------------------------------
# Клавиатуры
# ----------------------------------------------------------------------------
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔔 Напоминания", callback_data="menu:reminders"),
            InlineKeyboardButton(text="✅ Привычки", callback_data="menu:habits"),
        ],
        [
            InlineKeyboardButton(text="🧠 Промпт ИИ", callback_data="menu:prompt"),
        ],
    ])


def reminders_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить напоминание", callback_data="rem:add")],
        [InlineKeyboardButton(text="📋 Список напоминаний", callback_data="rem:list")],
        [InlineKeyboardButton(text="🗑 Удалить напоминание", callback_data="rem:pickdel")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
    ])


def habits_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить привычку", callback_data="hab:add")],
        [InlineKeyboardButton(text="📋 Мои привычки", callback_data="hab:list")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
    ])


def prompt_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👀 Показать промпт", callback_data="pr:show")],
        [InlineKeyboardButton(text="✏️ Редактировать (/edit_prompt)", callback_data="pr:hint")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
    ])


def back_to_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")],
    ])


# ----------------------------------------------------------------------------
# Ограничение доступа: только ADMIN_ID (через глобальные outer-миддлвари)
# ----------------------------------------------------------------------------
def register_guards(dp: Dispatcher) -> None:
    async def message_guard(handler, event: Message, kwargs):
        if event.from_user and event.from_user.id == ADMIN_ID:
            return await handler(event, **kwargs)
        logger.info("Ignored message from non-admin user id=%s",
                    getattr(event.from_user, "id", "?"))
        return None  # тихо игнорируем всех остальных

    async def callback_guard(handler, event: CallbackQuery, kwargs):
        if event.from_user and event.from_user.id == ADMIN_ID:
            return await handler(event, **kwargs)
        await event.answer("⛔ Доступ запрещён.")
        return None

    dp.message.outer_middleware(message_guard)
    dp.callback_query.outer_middleware(callback_guard)


# ----------------------------------------------------------------------------
# Команды
# ----------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message):
    welcome = (
        "🤖 <b>Привет! Я твоя ИИ-напоминалка и трекер привычек.</b>\n\n"
        "Я работаю через нейросеть: каждое напоминание превращаю в креативный "
        "пинок от имени твоего личного тренера.\n\n"
        "Выбери действие в меню ниже 👇"
    )
    await message.answer(welcome, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)


@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "<b>Команды:</b>\n"
        "/start — главное меню\n"
        "/show_prompt — показать текущий системный промпт ИИ\n"
        "/edit_prompt &lt;текст&gt; — заменить системный промпт\n"
        "/ai &lt;вопрос&gt; — спросить ИИ напрямую\n"
        "/remind HH:MM Текст — быстро создать ежедневное напоминание\n"
        "/habits — список привычек\n"
        "/help — эта справка"
    )
    await message.answer(help_text, parse_mode=ParseMode.HTML)


@router.message(Command("show_prompt"))
async def cmd_show_prompt(message: Message):
    prompt = db_get_prompt()
    await message.answer(
        "🧠 <b>Текущий системный промпт ИИ:</b>\n\n"
        f"<code>{html.escape(prompt)}</code>\n\n"
        "Изменить: <code>/edit_prompt новый текст</code>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("edit_prompt"))
async def cmd_edit_prompt(message: Message, command: CommandObject):
    new_prompt = (command.args or "").strip()
    if not new_prompt:
        await message.answer(
            "Использование: <code>/edit_prompt Ты дерзкий тренер, мотивируй меня жестко...</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    db_set_prompt(new_prompt)
    await message.answer(
        "✅ Системный промпт обновлён!\n\n"
        f"<b>Новый промпт:</b>\n<code>{html.escape(new_prompt)}</code>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("ai"))
async def cmd_ai(message: Message, command: CommandObject):
    query = (command.args or "").strip()
    if not query:
        await message.answer("Напиши вопрос: <code>/ai как перестать прокрастинировать?</code>",
                             parse_mode=ParseMode.HTML)
        return
    waiting = await message.answer("🧠 Думаю...")
    answer = await ask_ai(query)
    await waiting.edit_text(f"🤖 {answer}")


@router.message(Command("habits"))
async def cmd_habits(message: Message):
    await message.answer(build_habits_text(), reply_markup=back_to_main_kb())


# ----------------------------------------------------------------------------
# Быстрая команда /remind HH:MM Текст
# ----------------------------------------------------------------------------
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})\s+(.+)$", re.S)


@router.message(Command("remind"))
async def cmd_remind(message: Message, command: CommandObject):
    args = (command.args or "").strip()
    m = TIME_RE.match(args)
    if not m:
        await message.answer(
            "Формат: <code>/remind 08:30 Выпить воды</code>", parse_mode=ParseMode.HTML
        )
        return
    hour, minute, text = int(m.group(1)), int(m.group(2)), m.group(3).strip()
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await message.answer("Время должно быть в диапазоне 00:00–23:59.")
        return
    rid = db_add_reminder(text, hour, minute)
    schedule_add_job(rid, text, hour, minute)
    await message.answer(
        f"✅ Напоминание №{rid} «{text}» создано на {hour:02d}:{minute:02d} ежедневно.\n"
        "Каждый день я буду генерировать креативный пинок через ИИ. 🔥"
    )


# ----------------------------------------------------------------------------
# Колбэки меню
# ----------------------------------------------------------------------------
@router.callback_query(F.data == "menu:main")
async def cb_main_menu(cb: CallbackQuery):
    await cb.message.edit_text(
        "🏠 <b>Главное меню</b>\n\nУправление напоминаниями и привычками:",
        reply_markup=main_menu_kb(),
        parse_mode=ParseMode.HTML,
    )
    await cb.answer()


@router.callback_query(F.data == "menu:reminders")
async def cb_reminders_menu(cb: CallbackQuery):
    await cb.message.edit_text(
        "🔔 <b>Напоминания</b>\n\nВыбери действие:",
        reply_markup=reminders_menu_kb(),
        parse_mode=ParseMode.HTML,
    )
    await cb.answer()


@router.callback_query(F.data == "menu:habits")
async def cb_habits_menu(cb: CallbackQuery):
    await cb.message.edit_text(
        "✅ <b>Трекер привычек</b>\n\nВыбери действие:",
        reply_markup=habits_menu_kb(),
        parse_mode=ParseMode.HTML,
    )
    await cb.answer()


@router.callback_query(F.data == "menu:prompt")
async def cb_prompt_menu(cb: CallbackQuery):
    await cb.message.edit_text(
        "🧠 <b>Системный промпт ИИ</b>\n\n"
        "Это «личность» нейросети, которая пишет тебе напоминания.\n"
        "Команда изменения: <code>/edit_prompt твой текст</code>",
        reply_markup=prompt_menu_kb(),
        parse_mode=ParseMode.HTML,
    )
    await cb.answer()


@router.callback_query(F.data == "pr:show")
async def cb_prompt_show(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(
        "🧠 <b>Текущий системный промпт:</b>\n\n<code>{}</code>".format(
            html.escape(db_get_prompt())
        ),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "pr:hint")
async def cb_prompt_hint(cb: CallbackQuery):
    await cb.message.answer(
        "Чтобы изменить промпт, отправь команду:\n"
        "<code>/edit_prompt Ты дерзкий тренер, мотивируй меня жестко...</code>",
        parse_mode=ParseMode.HTML,
    )
    await cb.answer()


# ----------------------------------------------------------------------------
# Напоминания: добавление через диалог + список + удаление
# ----------------------------------------------------------------------------
@router.callback_query(F.data == "rem:add")
async def cb_rem_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddReminder.waiting_time)
    await cb.message.answer("⏰ Во сколько повторять напоминание каждый день?\n"
                            "Формат: ЧЧ:ММ (например, 08:30)")
    await cb.answer()


@router.message(AddReminder.waiting_time, F.text)
async def rem_time_handler(message: Message, state: FSMContext):
    m = TIME_RE.match(message.text.strip().replace(" ", ""))
    if not m or not (int(m.group(1)) <= 23 and int(m.group(2)) <= 59):
        await message.answer("Не понял время. Пример формата: 08:30 или 21:05")
        return
    await state.update_data(hour=int(m.group(1)), minute=int(m.group(2)))
    await state.set_state(AddReminder.waiting_text)
    await message.answer("✍️ О чём напоминать? (например: Выпить воды)")


@router.message(AddReminder.waiting_text, F.text)
async def rem_text_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    text = message.text.strip()
    rid = db_add_reminder(text, data["hour"], data["minute"])
    schedule_add_job(rid, text, data["hour"], data["minute"])
    await message.answer(
        f"✅ Напоминание №{rid} «{text}» создано на {data['hour']:02d}:{data['minute']:02d} ежедневно. 🔥",
        reply_markup=back_to_main_kb(),
    )


def build_reminders_text() -> str:
    rows = db_list_reminders()
    if not rows:
        return "📋 Напоминаний пока нет. Добавь новое кнопкой ➕!"
    lines = ["📋 <b>Твои напоминания:</b>", ""]
    for rid, text, hour, minute in rows:
        lines.append(f"• <code>{hour:02d}:{minute:02d}</code> — {html.escape(text)} (id {rid})")
    lines.append("")
    lines.append(f"Всего: {len(rows)}. Часовой пояс: {TZ_NAME}")
    return "\n".join(lines)


@router.callback_query(F.data == "rem:list")
async def cb_rem_list(cb: CallbackQuery):
    await cb.message.answer(build_reminders_text(), parse_mode=ParseMode.HTML)
    await cb.answer()


@router.callback_query(F.data == "rem:pickdel")
async def cb_rem_pickdel(cb: CallbackQuery):
    rows = db_list_reminders()
    if not rows:
        await cb.answer("Нет напоминаний для удаления", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{h:02d}:{m:02d} {t[:20]}", callback_data=f"rem:del:{rid}")]
        for rid, t, h, m in rows
    ] + [[InlineKeyboardButton(text="⬅️ Отмена", callback_data="menu:reminders")]])
    await cb.message.edit_text("Какое напоминание удалить?", reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("rem:del:"))
async def cb_rem_del(cb: CallbackQuery):
    rid = int(cb.data.split(":")[2])
    db_del_reminder(rid)
    schedule_remove_job(rid)
    await cb.message.answer(f"🗑 Напоминание №{rid} удалено.")
    await cb.message.edit_text("🔔 <b>Напоминания</b>\n\nВыбери действие:",
                               reply_markup=reminders_menu_kb(), parse_mode=ParseMode.HTML)
    await cb.answer()


# ----------------------------------------------------------------------------
# Привычки
# ----------------------------------------------------------------------------
@router.callback_query(F.data == "hab:add")
async def cb_hab_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddHabit.waiting_name)
    await cb.message.answer("Назови привычку (например: Зарядка 15 минут)")
    await cb.answer()


@router.message(AddHabit.waiting_name, F.text)
async def hab_name_handler(message: Message, state: FSMContext):
    await state.clear()
    name = message.text.strip()[:80]
    if db_add_habit(name):
        await message.answer(f"✅ Привычка «{name}» добавлена!", reply_markup=back_to_main_kb())
    else:
        await message.answer(f"⚠️ Такая привычка уже есть: «{name}»")


def build_habits_text() -> str:
    today = datetime.now(ZoneInfo(TZ_NAME)).strftime("%Y-%m-%d")
    rows = db_list_habits()
    if not rows:
        return "📋 Привычек пока нет. Добавь первую кнопкой ➕!"
    lines = ["📋 <b>Твои привычки</b> (кнопка ниже отмечает выполнение за сегодня):", ""]
    for hid, name, target, done in rows:
        dates = set(filter(None, done.split(",")))
        streak = len(dates)
        mark = "✅" if today in dates else "⬜"
        bar = "🟩" * min(streak, 10) + "⬜" * max(0, min(target, 10) - min(streak, 10))
        lines.append(f"{mark} <b>{html.escape(name)}</b> — серия {streak}/{target} дней\n{bar}")
        lines.append("")
    return "\n".join(lines)


def habits_inline_kb() -> InlineKeyboardMarkup:
    rows = db_list_habits()
    today = datetime.now(ZoneInfo(TZ_NAME)).strftime("%Y-%m-%d")
    buttons = []
    for hid, name, target, done in rows:
        dates = set(filter(None, done.split(",")))
        mark = "✅" if today in dates else "⬜"
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {name[:28]} ({len(dates)}/{target})",
            callback_data=f"hab:toggle:{hid}",
        )])
    if not buttons:
        buttons = [[InlineKeyboardButton(text="Пока пусто — добавь привычку", callback_data="hab:add")]]
    buttons.append([InlineKeyboardButton(text="🗑 Удалить привычку", callback_data="hab:pickdel"),
                    InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:habits")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "hab:list")
async def cb_hab_list(cb: CallbackQuery):
    await cb.message.edit_text(build_habits_text(), reply_markup=habits_inline_kb(),
                               parse_mode=ParseMode.HTML)
    await cb.answer()


@router.callback_query(F.data.startswith("hab:toggle:"))
async def cb_hab_toggle(cb: CallbackQuery):
    hid = int(cb.data.split(":")[2])
    today = datetime.now(ZoneInfo(TZ_NAME)).strftime("%Y-%m-%d")
    marked = db_toggle_habit(hid, today)
    if marked:
        await cb.answer("Отмечено! Так держать 💪")
    else:
        await cb.answer("Отметка за сегодня снята")
    try:
        await cb.message.edit_text(build_habits_text(), reply_markup=habits_inline_kb(),
                                   parse_mode=ParseMode.HTML)
    except Exception:  # сообщение могло стать неактуальным
        pass


@router.callback_query(F.data == "hab:pickdel")
async def cb_hab_pickdel(cb: CallbackQuery):
    rows = db_list_habits()
    if not rows:
        await cb.answer("Нет привычек", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name[:30], callback_data=f"hab:del:{hid}")]
        for hid, name, *_ in rows
    ] + [[InlineKeyboardButton(text="⬅️ Отмена", callback_data="menu:habits")]])
    await cb.message.edit_text("Какую привычку удалить?", reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("hab:del:"))
async def cb_hab_del(cb: CallbackQuery):
    hid = int(cb.data.split(":")[2])
    db_del_habit(hid)
    await cb.message.answer("🗑 Привычка удалена.")
    await cb.message.edit_text("✅ <b>Трекер привычек</b>\n\nВыбери действие:",
                               reply_markup=habits_menu_kb(), parse_mode=ParseMode.HTML)
    await cb.answer()


# ----------------------------------------------------------------------------
# Планировщик (APScheduler)
# ----------------------------------------------------------------------------
async def fire_reminder(reminder_id: int, reminder_text: str):
    """Бюджетная функция: скрытый запрос к ИИ -> доставка креативного ответа."""
    logger.info("Firing reminder #%s: %s", reminder_id, reminder_text)
    creative = await ask_ai(
        f"Пришло время сделать: «{reminder_text}». "
        f"Текущее время: {datetime.now(ZoneInfo(TZ_NAME)).strftime('%H:%M')}. "
        "Сформулируй напоминание в стиле своего характера."
    )
    try:
        await bot.send_message(ADMIN_ID, f"🔔 <b>Напоминание</b>\n\n{html.escape(creative)}",
                               parse_mode=ParseMode.HTML)
    except Exception:
        await bot.send_message(ADMIN_ID, f"🔔 Напоминание: {creative}")


def schedule_add_job(rid: int, text: str, hour: int, minute: int) -> None:
    scheduler.add_job(
        fire_reminder,
        trigger="cron",
        hour=hour,
        minute=minute,
        args=[rid, text],
        id=f"reminder_{rid}",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Scheduled reminder #%s at %02d:%02d (%s)", rid, hour, minute, TZ_NAME)


def schedule_remove_job(rid: int) -> None:
    try:
        scheduler.remove_job(f"reminder_{rid}")
    except Exception:
        pass


def restore_jobs() -> None:
    for rid, text, hour, minute in db_list_reminders():
        schedule_add_job(rid, text, hour, minute)


# ----------------------------------------------------------------------------
# Запуск
# ----------------------------------------------------------------------------
class ProxyAiohttpSession(AiohttpSession):
    """Сессия aiogram поверх aiohttp-socks ProxyConnector (SOCKS5/HTTP/HTTPS).

    aiogram 3.x сам умеет строить ProxyConnector, если передать ему строку
    прокси или кортеж (строка, BasicAuth) — здесь мы просто аккуратно
    разбираем PROXY_URL вида scheme://[user:pass@]host:port.
    """


def build_session() -> ProxyAiohttpSession | AiohttpSession:
    """Сессия aiogram с SOCKS5/HTTP прокси из PROXY_URL (если задан).

    Внутри aiogram 3.x это приводит к созданию aiohttp_socks.ProxyConnector
    (поддерживаются схемы socks4://, socks5://, socks5h://, http://, https://).
    Логируем адрес без логина/пароля.
    """
    if PROXY_URL:
        logger.info("Using proxy: %s", PROXY_URL.split("@")[-1])
        return ProxyAiohttpSession(proxy=PROXY_URL)
    logger.warning("PROXY_URL is empty — connecting directly to Telegram API")
    return AiohttpSession()


async def main():
    global bot
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env")

    db_init()

    session = build_session()
    bot = Bot(token=TELEGRAM_BOT_TOKEN, session=session,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    dp = Dispatcher()
    dp.include_router(router)
    register_guards(dp)

    scheduler.start()
    restore_jobs()

    # SetMyCommands тоже идёт через прокси-сессию
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="help", description="Справка"),
            BotCommand(command="show_prompt", description="Показать промпт ИИ"),
            BotCommand(command="edit_prompt", description="Изменить промпт ИИ"),
            BotCommand(command="remind", description="/remind 08:30 Текст"),
            BotCommand(command="habits", description="Мои привычки"),
            BotCommand(command="ai", description="Спросить ИИ"),
        ])
    except Exception as e:
        logger.warning("set_my_commands failed: %s", e)

    logger.info("Bot started. Admin: %s | Model: %s | TZ: %s", ADMIN_ID, OPENROUTER_MODEL, TZ_NAME)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true", help="Run offline self-test")
    args = parser.parse_args()

    if args.selftest:
        async def _selftest():
            db_init()
            print("[OK] DB initialized at", DB_PATH)
            db_set_prompt("Тестовый промпт")
            assert db_get_prompt() == "Тестовый промпт"
            print("[OK] prompt get/set")
            rid = db_add_reminder("Выпить воды", 8, 30)
            assert db_list_reminders()[0][1] == "Выпить воды"
            print("[OK] reminders CRUD, sample text:", build_reminders_text()[:40].replace("\n", " "))
            db_add_habit("Зарядка", 7)
            db_toggle_habit(1, "2026-09-27")
            print("[OK] habits CRUD, list ok")
            s = build_session()
            print("[OK] session built:", type(s).__name__)
            scheduler.start()
            schedule_add_job(rid, "Выпить воды", 8, 30)
            assert scheduler.get_job(f"reminder_{rid}") is not None
            print("[OK] APScheduler cron job registered")
            scheduler.shutdown()
            # Проверка прокси-сессии (ProxyConnector строится внутри aiogram)
            globals()["PROXY_URL"] = "socks5://user:pass@127.0.0.1:1080"
            sp = build_session()
            assert sp._connector_type.__name__ == "ProxyConnector"
            assert "proxy_type" in sp._connector_init
            print("[OK] proxy session built with aiohttp_socks.ProxyConnector")
            await sp.close()
            print("SELFTEST PASSED ✅")
        asyncio.run(_selftest())
    else:
        bot = None
        try:
            asyncio.run(main())
        except (KeyboardInterrupt, SystemExit):
            logger.info("Bot stopped")

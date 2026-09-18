"""Telegram-бот с расписанием группы ПЛ-3-24-02 (РАНХиГС СПб).

Версия для GitHub Actions: бот запускается по расписанию, несколько минут отвечает
на сообщения, сохраняет состояние в папку state/ и выключается.
Локально (без RUN_SECONDS) работает бесконечно, как обычный бот.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)
from dotenv import load_dotenv

from schedule import (MY_GROUP_TITLE, OPTIONAL_GROUPS, SCHEDULE_URL, Lesson, filter_mine,
                      format_day, format_week, parse_schedule, split_message, week_start)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PROXY = os.getenv("PROXY")                       # необязательно: прокси для Telegram
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "0"))  # 0 = работать бесконечно
TZ = ZoneInfo("Europe/Moscow")                   # Санкт-Петербург

STATE_DIR = Path(__file__).parent / "state"
SCHEDULE_FILE = STATE_DIR / "schedule.json"      # кэш расписания с сайта
SETTINGS_FILE = STATE_DIR / "settings.json"      # выбранные элективы пользователей
SCHEDULE_TTL = 6 * 3600                          # обновлять расписание раз в 6 часов

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
dp = Dispatcher()


# ─────────────────────────── РАСПИСАНИЕ (кэш в файле) ───────────────────────────
_lessons: list[Lesson] | None = None
_lock = asyncio.Lock()


def _load_schedule_file() -> tuple[list[Lesson] | None, float]:
    try:
        data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        lessons = [Lesson(**{**d, "day": date.fromisoformat(d["day"])}) for d in data["lessons"]]
        return lessons, float(data["fetched_at"])
    except (FileNotFoundError, KeyError, ValueError, TypeError):
        return None, 0.0


def _save_schedule_file(lessons: list[Lesson]):
    STATE_DIR.mkdir(exist_ok=True)
    data = {
        "fetched_at": time.time(),
        "lessons": [{**asdict(l), "day": l.day.isoformat()} for l in lessons],
    }
    SCHEDULE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


async def _download() -> str:
    headers = {"User-Agent": "Mozilla/5.0 (schedule-bot)"}
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
        async with s.get(SCHEDULE_URL) as resp:
            resp.raise_for_status()
            return await resp.text()


async def get_lessons() -> list[Lesson]:
    """Все строки таблицы. Берём из файла, а с сайта скачиваем, если кэш старше 6 часов."""
    global _lessons
    async with _lock:
        if _lessons is not None:
            return _lessons
        cached, fetched_at = _load_schedule_file()
        if cached is not None and time.time() - fetched_at < SCHEDULE_TTL:
            _lessons = cached
            return _lessons
        try:
            lessons = parse_schedule(await _download())
            _save_schedule_file(lessons)
            _lessons = lessons
            logging.info("Расписание скачано с сайта: %d строк", len(lessons))
        except Exception:
            logging.exception("Не удалось скачать расписание с сайта")
            if cached is None:
                raise
            _lessons = cached
            logging.info("Использую сохранённую копию расписания")
        return _lessons


# ─────────────────────────── НАСТРОЙКИ ПОЛЬЗОВАТЕЛЕЙ ───────────────────────────
# Репозиторий публичный, поэтому вместо Telegram ID храним их хэш —
# по файлу нельзя узнать, кто пользуется ботом.
def _uid(user_id: int) -> str:
    return hmac.new(BOT_TOKEN.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()[:16]


def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


_settings: dict = _load_settings()


def _save_settings():
    STATE_DIR.mkdir(exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(_settings, ensure_ascii=False, indent=1), encoding="utf-8")


def enabled_optional(user_id: int) -> set[str]:
    return set(_settings.get(_uid(user_id), []))


def toggle_optional(user_id: int, key: str) -> bool:
    current = enabled_optional(user_id)
    current.symmetric_difference_update({key})
    if current:
        _settings[_uid(user_id)] = sorted(current)
    else:
        _settings.pop(_uid(user_id), None)
    _save_settings()
    return key in current


async def user_lessons(user_id: int) -> list[Lesson]:
    return filter_mine(await get_lessons(), enabled_optional(user_id))


def today() -> date:
    return datetime.now(TZ).date()


# ─────────────────────────── КЛАВИАТУРЫ ───────────────────────────
BTN_TODAY, BTN_TOMORROW = "📅 Сегодня", "➡️ Завтра"
BTN_WEEK, BTN_NEXT_WEEK = "🗓 Эта неделя", "⏭ След. неделя"
BTN_SETTINGS = "⚙️ Настройки"

main_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TOMORROW)],
              [KeyboardButton(text=BTN_WEEK), KeyboardButton(text=BTN_NEXT_WEEK)],
              [KeyboardButton(text=BTN_SETTINGS)]],
    resize_keyboard=True,
)


def day_nav(d: date) -> InlineKeyboardMarkup:
    prev_d, next_d = d - timedelta(days=1), d + timedelta(days=1)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️", callback_data=f"day:{prev_d.isoformat()}"),
        InlineKeyboardButton(text="Сегодня", callback_data=f"day:{today().isoformat()}"),
        InlineKeyboardButton(text="▶️", callback_data=f"day:{next_d.isoformat()}"),
    ]])


def week_nav(monday: date) -> InlineKeyboardMarkup:
    prev_w, next_w = monday - timedelta(days=7), monday + timedelta(days=7)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Пред.", callback_data=f"week:{prev_w.isoformat()}"),
        InlineKeyboardButton(text="След. ▶️", callback_data=f"week:{next_w.isoformat()}"),
    ]])


def settings_kb(user_id: int) -> InlineKeyboardMarkup:
    enabled = enabled_optional(user_id)
    rows = [[InlineKeyboardButton(
        text=f"{'✅' if key in enabled else '⬜️'} {title}",
        callback_data=f"toggle:{key}",
    )] for key, (_, title) in OPTIONAL_GROUPS.items()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


SETTINGS_TEXT = (
    "⚙️ <b>Настройки</b>\n\n"
    "Отметь элективы, на которые ты ходишь, — их пары появятся в расписании.\n"
    "Нажми ещё раз, чтобы убрать."
)
ERROR_TEXT = "😔 Не получилось загрузить расписание с сайта. Попробуй чуть позже."


async def safe_answer(callback: CallbackQuery, text: str | None = None):
    """Старые нажатия (пока бот «спал») Telegram не даёт подтвердить — это не страшно."""
    try:
        await callback.answer(text)
    except TelegramBadRequest:
        pass


# ─────────────────────────── ОТПРАВКА ───────────────────────────
async def send_day(message: Message, d: date):
    try:
        lessons = await user_lessons(message.from_user.id)
    except Exception:
        return await message.answer(ERROR_TEXT)
    await message.answer(format_day(d, lessons, today()), reply_markup=day_nav(d))


async def send_week(message: Message, monday: date):
    try:
        lessons = await user_lessons(message.from_user.id)
    except Exception:
        return await message.answer(ERROR_TEXT)
    chunks = split_message(format_week(monday, lessons, today()))
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await message.answer(chunk, reply_markup=week_nav(monday) if is_last else None)


# ─────────────────────────── ХЕНДЛЕРЫ ───────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        f"👋 Привет! Я показываю расписание группы <b>{MY_GROUP_TITLE}</b>\n"
        "с учётом подгрупп по иностранному и второму иностранному.\n\n"
        "Жми кнопки внизу или используй команды:\n"
        "/today — сегодня\n/tomorrow — завтра\n/week — эта неделя\n/nextweek — следующая неделя\n"
        "/settings — выбрать элективы\n\n"
        "⏳ Я живу на бесплатном сервере и проверяю сообщения раз в несколько минут, "
        "так что иногда отвечаю с задержкой.",
        reply_markup=main_kb,
    )


@dp.message(Command("today"))
@dp.message(F.text == BTN_TODAY)
async def cmd_today(message: Message):
    await send_day(message, today())


@dp.message(Command("tomorrow"))
@dp.message(F.text == BTN_TOMORROW)
async def cmd_tomorrow(message: Message):
    await send_day(message, today() + timedelta(days=1))


@dp.message(Command("week"))
@dp.message(F.text == BTN_WEEK)
async def cmd_week(message: Message):
    await send_week(message, week_start(today()))


@dp.message(Command("nextweek"))
@dp.message(F.text == BTN_NEXT_WEEK)
async def cmd_next_week(message: Message):
    await send_week(message, week_start(today()) + timedelta(days=7))


@dp.message(Command("settings"))
@dp.message(F.text == BTN_SETTINGS)
async def cmd_settings(message: Message):
    await message.answer(SETTINGS_TEXT, reply_markup=settings_kb(message.from_user.id))


@dp.callback_query(F.data.startswith("toggle:"))
async def on_toggle(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    if key not in OPTIONAL_GROUPS:
        return await safe_answer(callback)
    now_on = toggle_optional(callback.from_user.id, key)
    title = OPTIONAL_GROUPS[key][1]
    try:
        await callback.message.edit_reply_markup(reply_markup=settings_kb(callback.from_user.id))
    except TelegramBadRequest:
        pass
    await safe_answer(callback, f"«{title}» {'добавлен в расписание' if now_on else 'убран из расписания'}")


@dp.callback_query(F.data.startswith(("day:", "week:")))
async def on_nav(callback: CallbackQuery):
    kind, iso = callback.data.split(":", 1)
    d = date.fromisoformat(iso)
    try:
        lessons = await user_lessons(callback.from_user.id)
    except Exception:
        return await safe_answer(callback, "Сайт недоступен, попробуй позже")

    if kind == "day":
        text, kb = format_day(d, lessons, today()), day_nav(d)
    else:
        text, kb = format_week(d, lessons, today()), week_nav(d)

    chunks = split_message(text)
    try:
        if len(chunks) == 1:
            await callback.message.edit_text(text, reply_markup=kb)
        else:
            for i, chunk in enumerate(chunks):
                await callback.message.answer(chunk, reply_markup=kb if i == len(chunks) - 1 else None)
    except TelegramBadRequest:
        pass  # текст не изменился — ничего страшного
    await safe_answer(callback)


# ─────────────────────────── ГЛАВНЫЙ ЦИКЛ ───────────────────────────
async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не найден BOT_TOKEN (в GitHub — Settings → Secrets → Actions).")

    session = AiohttpSession(proxy=PROXY) if PROXY else None
    bot = Bot(BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    # Обновляем кэш расписания заранее, даже если сообщений не будет
    try:
        await get_lessons()
    except Exception:
        pass

    deadline = time.monotonic() + RUN_SECONDS if RUN_SECONDS else None
    offset = None
    logging.info("Бот запущен%s", f" на {RUN_SECONDS} с" if RUN_SECONDS else "")
    try:
        while True:
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 1:
                    break
                wait = int(min(25, left))
            else:
                wait = 25
            try:
                updates = await bot.get_updates(offset=offset, timeout=wait,
                                                allowed_updates=["message", "callback_query"])
            except TelegramNetworkError as e:
                logging.warning("Сеть: %s", e)
                await asyncio.sleep(3)
                continue
            for update in updates:
                offset = update.update_id + 1
                try:
                    await dp.feed_update(bot, update)
                except Exception:
                    logging.exception("Ошибка при обработке сообщения")

        # Сообщаем Telegram, что всё полученное обработано, иначе следующий запуск ответит повторно
        if offset is not None:
            await bot.get_updates(offset=offset, timeout=0)
        logging.info("Время вышло, бот завершает работу")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

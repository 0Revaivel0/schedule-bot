"""Парсинг расписания с сайта РАНХиГС СПб и красивое форматирование для Telegram."""
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape

from bs4 import BeautifulSoup

# ─────────────────────────── НАСТРОЙКИ ───────────────────────────
SCHEDULE_URL = "https://spb.ranepa.ru/raspisanie/pl-3-24-01-03/"

# Какие строки таблицы считать «своими». Сравнение без учёта регистра и пробелов.
MY_GROUPS = {
    "ПЛ-3-24-02",           # основная группа
    "ПЛ-3-24-01-02",        # поток групп 01–02 (в него входит 02)
    "ПЛ-3-24-01-03",        # поток групп 01–03
    "ПЛ-3-24-01-03/3",      # иностранный язык
    "ПЛ-3-24-01-03/1нем",   # второй иностранный (немецкий)
}

# Необязательные группы (электив и т.п.) — каждый пользователь включает их сам в настройках.
# ключ → (код группы на сайте, название для кнопки)
OPTIONAL_GROUPS = {
    "kur": ("ПЛ-3-24-01-02/КУР", "Концепции устойчивого развития"),
}
MY_GROUP_TITLE = "ПЛ-3-24-02"

WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]

# Заголовок столбца на сайте → поле
COLUMNS = {
    "дата": "date",
    "время": "time",
    "тип занятия": "kind",
    "группа": "group",
    "наименование дисциплины": "subject",
    "преподаватель": "teacher",
    "аудитория": "room",
}


@dataclass(frozen=True)
class Lesson:
    day: date
    start: str
    end: str
    kind: str
    group: str
    subject: str
    teacher: str
    room: str


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).casefold()


_MY = {_norm(g) for g in MY_GROUPS}


def is_mine(group: str, enabled_optional: set[str] = frozenset()) -> bool:
    g = _norm(group)
    if g in _MY:
        return True
    return any(g == _norm(OPTIONAL_GROUPS[key][0])
               for key in enabled_optional if key in OPTIONAL_GROUPS)


def _split_time(s: str) -> tuple[str, str]:
    parts = re.findall(r"(\d{1,2})[.:](\d{2})", s)
    if len(parts) < 2:
        return s.strip(), ""
    (h1, m1), (h2, m2) = parts[:2]
    return f"{int(h1):02d}:{m1}", f"{int(h2):02d}:{m2}"


# ─────────────────────────── ПАРСИНГ ───────────────────────────
def parse_schedule(html: str) -> list[Lesson]:
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        if "дата" not in headers or "преподаватель" not in headers:
            continue
        idx = {COLUMNS[h]: i for i, h in enumerate(headers) if h in COLUMNS}

        lessons: list[Lesson] = []
        for tr in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) < len(headers):
                continue
            try:
                day = datetime.strptime(cells[idx["date"]], "%d.%m.%Y").date()
            except ValueError:
                continue
            start, end = _split_time(cells[idx["time"]])
            get = lambda key: cells[idx[key]] if key in idx else ""
            lessons.append(Lesson(day, start, end, get("kind"), get("group"),
                                  get("subject"), get("teacher"), get("room")))
        return lessons
    raise ValueError("На странице не найдена таблица расписания")


def filter_mine(lessons: list[Lesson], enabled_optional: set[str] = frozenset()) -> list[Lesson]:
    seen, result = set(), []
    for l in lessons:
        key = (l.day, l.start, l.subject, l.group)
        if is_mine(l.group, enabled_optional) and key not in seen:
            seen.add(key)
            result.append(l)
    return sorted(result, key=lambda l: (l.day, l.start))


# ─────────────────────────── ФОРМАТИРОВАНИЕ ───────────────────────────
def _kind_label(kind: str) -> str:
    k = kind.lower()
    if "лекц" in k:
        return "📘 Лекция"
    if "практ" in k or "семинар" in k:
        return "✏️ Практика"
    if "лаб" in k:
        return "🧪 Лабораторная"
    if "экзам" in k:
        return "🎓 Экзамен"
    if "зач" in k:
        return "✅ Зачёт"
    if "консульт" in k:
        return "💬 Консультация"
    return f"📌 {kind}" if kind else "📌 Занятие"


def _room_label(room: str) -> str:
    if "сдо" in room.lower():
        return "💻 Онлайн (СДО)"
    return f"📍 {room}" if room else "📍 —"


def _group_note(group: str) -> str:
    g = _norm(group)
    if g == _norm(MY_GROUP_TITLE):
        return ""
    if any(g == _norm(code) for code, _ in OPTIONAL_GROUPS.values()):
        return f"⭐ Электив · {group}"
    if "/" in group:
        return f"👥 Подгруппа {group}"
    return f"👥 Поток {group}"


def day_title(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def format_lesson(l: Lesson, num: int) -> str:
    head = f"<b>{num} пара</b> · "
    time_str = f"{l.start}–{l.end}" if l.end else l.start
    lines = [
        f"{head}🕐 <code>{time_str}</code>",
        f"<b>{escape(l.subject)}</b>",
        _kind_label(l.kind),
        f"👤 {escape(l.teacher)}" if l.teacher else "",
        _room_label(escape(l.room)),
        _group_note(escape(l.group)),
    ]
    return "\n".join(x for x in lines if x)


def format_day(d: date, lessons: list[Lesson], today: date | None = None) -> str:
    marker = " · <i>сегодня</i>" if today and d == today else ""
    header = f"📅 <b>{day_title(d)}</b>{marker}"
    day_lessons = [l for l in lessons if l.day == d]
    if not day_lessons:
        return f"{header}\n\n🎉 Пар нет — отдыхаем!"
    # Нумеруем пары по порядку в этот день: первая по времени — «1 пара», и т.д.
    # Если в одно время стоят два занятия, у них будет один номер.
    starts = sorted({l.start for l in day_lessons})
    number = {start: i + 1 for i, start in enumerate(starts)}
    body = "\n\n".join(format_lesson(l, number[l.start]) for l in day_lessons)
    return f"{header}\n━━━━━━━━━━━━━━━\n{body}"


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def format_week(monday: date, lessons: list[Lesson], today: date | None = None) -> str:
    sunday = monday + timedelta(days=6)
    title = (f"🗓 <b>Неделя {monday.day} {MONTHS[monday.month - 1]} – "
             f"{sunday.day} {MONTHS[sunday.month - 1]}</b>\n👥 {MY_GROUP_TITLE}")
    blocks = []
    for i in range(7):
        d = monday + timedelta(days=i)
        if any(l.day == d for l in lessons):
            blocks.append(format_day(d, lessons, today))
    if not blocks:
        return f"{title}\n\n🎉 На этой неделе пар нет."
    return title + "\n\n" + "\n\n\n".join(blocks)


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Telegram ограничивает сообщение 4096 символами — режем по дням."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for block in text.split("\n\n\n"):
        candidate = f"{current}\n\n\n{block}" if current else block
        if len(candidate) > limit and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks

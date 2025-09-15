import os
import json
import random
import re
from datetime import datetime, date, timedelta
from math import ceil
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from dotenv import load_dotenv

# =====================
#   ЗАГРУЗКА НАСТРОЕК
# =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROUP_ID = int(os.getenv("GROUP_ID", "0"))
TZ = ZoneInfo(os.getenv("TZ", "Europe/Moscow"))

# список админов через запятую: "2037697119,6103764666"
ADMINS = {int(x) for x in os.getenv("ADMINS", "").replace(" ", "").split(",") if x}

if not TOKEN or GROUP_ID == 0 or not ADMINS:
    raise RuntimeError("Заполни .env: BOT_TOKEN, GROUP_ID, ADMINS")

# =====================
#    ПУТИ И ФАЙЛЫ
# =====================
START_DATE_FILE   = os.path.join(BASE_DIR, "start_date.txt")   # старт ротации
EXCEPTIONS_FILE   = os.path.join(BASE_DIR, "exceptions.json")  # подмены на даты
DEBTORS_FILE      = os.path.join(BASE_DIR, "debtors.json")     # должники (индексы или имена)
SIM_DATE_FILE     = os.path.join(BASE_DIR, "sim_date.txt")     # «симулируемая» дата для тестов
STUDENTS_FILE     = os.path.join(BASE_DIR, "students.txt")     # список студентов (Фамилия Имя [Отчество])
SCHEDULE_FILE     = os.path.join(BASE_DIR, "schedule.json")    # расписание (по дням недели/датам)

os.makedirs(BASE_DIR, exist_ok=True)

# =====================
#      УТИЛИТЫ I/O
# =====================
def load_text_lines(fname: str) -> list[str]:
    if not os.path.exists(fname):
        return []
    with open(fname, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]

def save_text_lines(fname: str, lines: list[str]):
    with open(fname, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))

def load_json(fname: str, default):
    if os.path.exists(fname):
        try:
            with open(fname, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default

def save_json(fname: str, data):
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_start_date() -> date:
    if os.path.exists(START_DATE_FILE):
        try:
            s = open(START_DATE_FILE, "r", encoding="utf-8").read().strip()
            return datetime.strptime(s, "%Y-%m-%d").date()
        except Exception:
            pass
    # дефолт — первое число текущего месяца
    today = datetime.now(TZ).date()
    return date(today.year, today.month, 1)

def save_start_date(d: date):
    with open(START_DATE_FILE, "w", encoding="utf-8") as f:
        f.write(d.strftime("%Y-%m-%d"))

def load_sim_date() -> date | None:
    if os.path.exists(SIM_DATE_FILE):
        try:
            s = open(SIM_DATE_FILE, "r", encoding="utf-8").read().strip()
            return datetime.strptime(s, "%Y-%m-%d").date()
        except Exception:
            return None
    return None

def save_sim_date(d: date | None):
    if d is None:
        if os.path.exists(SIM_DATE_FILE):
            try:
                os.remove(SIM_DATE_FILE)
            except Exception:
                pass
    else:
        with open(SIM_DATE_FILE, "w", encoding="utf-8") as f:
            f.write(d.strftime("%Y-%m-%d"))

# =====================
#  ЗАГРУЗКА СТУДЕНТОВ
# =====================
def _canon_name(s: str) -> str:
    # Нормализация: берём первые 2 слова (фамилия + имя), нижний регистр, ё→е, двойные пробелы -> один
    s = re.sub(r"\s+", " ", s.strip())
    parts = s.split(" ")
    core = " ".join(parts[:2]) if len(parts) >= 2 else s
    return core.lower().replace("ё", "е")

def load_students() -> list[str]:
    lines = load_text_lines(STUDENTS_FILE)
    # фильтруем дубли, нормализуем регистр/пробелы, но отображаем оригинал (Фамилия Имя [Отчество])
    seen = set()
    res = []
    for ln in lines:
        key = _canon_name(ln)
        if key and key not in seen:
            seen.add(key)
            res.append(ln.strip())
    return res

DUTY_LIST: list[str] = load_students()
if not DUTY_LIST:
    # падать не будем — создадим заглушку
    DUTY_LIST = ["Иванов Иван", "Петров Пётр"]

def name_to_idx(name: str) -> int:
    key = _canon_name(name)
    for i, n in enumerate(DUTY_LIST):
        if _canon_name(n) == key:
            return i
    raise ValueError(f"Не найдено в списке: {name}")

def try_resolve_name(inp: str) -> str | None:
    key = _canon_name(inp)
    for n in DUTY_LIST:
        if _canon_name(n) == key:
            return n
    # частичное совпадение: если введено только фамилия/часть
    for n in DUTY_LIST:
        if key and key in _canon_name(n):
            return n
    return None

def idx_to_name(idx: int) -> str:
    return DUTY_LIST[idx % len(DUTY_LIST)]

# =====================
#      ГЛОБАЛЬНОЕ
# =====================
START_DATE = load_start_date()
exceptions: dict[str, list[str]] = load_json(EXCEPTIONS_FILE, {})
_raw_debtors = load_json(DEBTORS_FILE, [])
debtors: list[int] = []
for item in _raw_debtors:
    if isinstance(item, int):
        debtors.append(item)
    elif isinstance(item, str):
        try:
            debtors.append(name_to_idx(item))
        except Exception:
            pass
save_json(DEBTORS_FILE, debtors)

# расписание
_schedule = load_json(SCHEDULE_FILE, {
    "mon": [], "tue": [], "wed": [], "thu": [], "fri": [], "sat": [],
    "dates": {}
})

# =====================
#    УТИЛИТЫ ДАТ
# =====================
def get_today() -> date:
    return datetime.now(TZ).date()

def is_sunday(d: date) -> bool:
    return d.weekday() == 6

def next_workday(d: date) -> date:
    t = d
    while True:
        t += timedelta(days=1)
        if not is_sunday(t):
            return t

def prev_workday(d: date) -> date:
    t = d
    while True:
        t -= timedelta(days=1)
        if not is_sunday(t):
            return t

def working_days_count(d0: date, d1: date) -> int:
    """Кол-во Пн–Сб между d0 (включ) и d1 (не включ)."""
    if d1 <= d0:
        return 0
    days = (d1 - d0).days
    full_weeks, rest = divmod(days, 7)
    cnt = full_weeks * 6
    for i in range(rest):
        wd = (d0.weekday() + i) % 7
        if wd != 6:
            cnt += 1
    return cnt

def fmt_ymd(d: date) -> str:
    return d.strftime("%Y-%m-%d")

def fmt_ddmmyyyy(d: date) -> str:
    return d.strftime("%d.%m.%Y")

# =====================
#  ПАРЫ ДЕЖУРНЫХ
# =====================
def base_pair(for_date: date) -> list[str]:
    # пары: (0,1), (2,3), ...
    steps = working_days_count(START_DATE, for_date)
    m = ceil(len(DUTY_LIST) / 2)
    pair_index = steps % m
    i = (2 * pair_index) % len(DUTY_LIST)
    j = (i + 1) % len(DUTY_LIST)
    return [DUTY_LIST[i], DUTY_LIST[j]]

def get_pair(for_date: date) -> list[str]:
    s = fmt_ymd(for_date)
    return exceptions.get(s, base_pair(for_date))

def set_exception(for_date: date, pair: list[str]):
    s = fmt_ymd(for_date)
    exceptions[s] = pair
    save_json(EXCEPTIONS_FILE, exceptions)

# =====================
#  РАСПИСАНИЕ УРОКОВ
# =====================
WEEKDAY_KEYS = ["mon","tue","wed","thu","fri","sat","sun"]
WEEKDAY_MAP_RU = {
    "пн":"mon","пон":"mon","понедельник":"mon",
    "вт":"tue","вторник":"tue",
    "ср":"wed","среда":"wed",
    "чт":"thu","четверг":"thu",
    "пт":"fri","пятница":"fri",
    "сб":"sat","суббота":"sat",
    "вс":"sun","воскресенье":"sun",
}

def schedule_for_date(d: date) -> list[str]:
    # приоритет: конкретная дата, иначе по дню недели
    key = fmt_ymd(d)
    if "dates" in _schedule and key in _schedule["dates"]:
        return _schedule["dates"][key]
    wd = d.weekday()  # 0-пн … 6-вс
    wd_key = WEEKDAY_KEYS[wd]
    return _schedule.get(wd_key, [])

def format_schedule(d: date) -> str:
    items = schedule_for_date(d)
    if not items:
        return "Расписание: не задано."
    lines = [f"Расписание ({fmt_ddmmyyyy(d)}):"]
    for idx, item in enumerate(items, 1):
        lines.append(f"{idx}. {item}")
    return "\n".join(lines)

def set_weekday_schedule(day_key: str, subjects: list[str]) -> None:
    key = day_key.lower()
    key = WEEKDAY_MAP_RU.get(key, key)
    if key not in WEEKDAY_KEYS:
        raise ValueError("Неверный день недели")
    _schedule[key] = subjects
    save_json(SCHEDULE_FILE, _schedule)

# =====================
#     ДОЛЖНИКИ
# =====================
def add_debtor_idx(idx: int):
    if idx not in debtors:
        debtors.append(idx)
        save_json(DEBTORS_FILE, debtors)

def pop_debtor_idx(idx: int):
    if idx in debtors:
        debtors.remove(idx)
        save_json(DEBTORS_FILE, debtors)

def next_replacement(absent_idx: int, current_pair_names: list[str]) -> int:
    n = len(DUTY_LIST)
    for k in range(1, n + 1):
        cand = (absent_idx + k) % n
        if DUTY_LIST[cand] not in current_pair_names:
            return cand
    return (absent_idx + 1) % n

# перенос «снятого» на следующий рабочий день
def carry_over_person_to_next_day(person: str, from_date: date):
    N = next_workday(from_date)
    tried = 0
    while tried < 180:
        base = base_pair(N)
        key = fmt_ymd(N)
        if person in base:
            return
        if key not in exceptions:
            partner = base[0] if base[0] != person else base[1]
            if partner == person:
                # найдём ближайшего другого
                idx = name_to_idx(person)
                for _ in range(len(DUTY_LIST)):
                    idx = (idx + 1) % len(DUTY_LIST)
                    cand = DUTY_LIST[idx]
                    if cand != person:
                        partner = cand
                        break
            set_exception(N, [person, partner])
            return
        else:
            if person in exceptions[key]:
                return
        N = next_workday(N)
        tried += 1

# =====================
#     ИНТЕРФЕЙС
# =====================
def build_keyboard(for_date: date, pair_names: list[str]) -> types.InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    dstr = for_date.strftime("%Y%m%d")
    for name in pair_names:
        idx = name_to_idx(name)
        kb.button(text=f"✅ {name}", callback_data=f"ok:{idx}:{dstr}")
        kb.button(text=f"❌ {name}", callback_data=f"no:{idx}:{dstr}")
    kb.button(text="🧨 Полный ресет", callback_data="wipe:all")
    kb.adjust(2, 2, 1)
    return kb.as_markup()

def render_text(for_date: date) -> str:
    p = get_pair(for_date)
    return f"Сегодня {fmt_ddmmyyyy(for_date)}\n🧹 Дежурные: {p[0]} и {p[1]}"

async def send_and_pin(bot: Bot, for_date: date | None = None):
    if for_date is None:
        for_date = get_today()
    pair = get_pair(for_date)
    text = render_text(for_date) + "\n\n" + format_schedule(for_date)
    msg = await bot.send_message(GROUP_ID, text, reply_markup=build_keyboard(for_date, pair), parse_mode=ParseMode.HTML)
    try:
        await bot.pin_chat_message(GROUP_ID, msg.message_id, disable_notification=True)
    except Exception:
        pass

# =====================
#   CALLBACK-КНОПКИ
# =====================
async def on_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("пошел вон", show_alert=True)
        return

    data = callback.data
    if data == "wipe:all":
        global START_DATE, exceptions, debtors
        START_DATE = get_today()
        save_start_date(START_DATE)
        exceptions = {}
        debtors = []
        save_json(EXCEPTIONS_FILE, exceptions)
        save_json(DEBTORS_FILE, debtors)
        save_sim_date(None)
        await callback.message.answer("бам бум.")
        await send_and_pin(callback.bot, get_today())
        await callback.answer()
        return

    try:
        action, idx_s, dstr = data.split(":")
        idx = int(idx_s)
        act_date = datetime.strptime(dstr, "%Y%m%d").date()
    except Exception:
        await callback.answer("Ошибка данных", show_alert=True)
        return

    pair = get_pair(act_date)

    if action == "ok":
        await callback.message.answer(f"✅ {idx_to_name(idx)} отметил как присутствующего")
        await callback.answer()
        return

    if action == "no":
        absent_name = idx_to_name(idx)
        await callback.message.answer(f"❌ {absent_name} отмечен как отсутствующий")
        add_debtor_idx(idx)
        repl_idx = next_replacement(idx, pair)
        repl_name = idx_to_name(repl_idx)
        new_pair = [repl_name if x == absent_name else x for x in pair]
        set_exception(act_date, new_pair)
        await callback.message.edit_text(render_text(act_date) + "\n\n" + format_schedule(act_date),
                                         reply_markup=build_keyboard(act_date, new_pair))
        await callback.answer()
        return

# =====================
#      КОМАНДЫ
# =====================
async def cmd_test(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    await send_and_pin(message.bot, get_today())
    await message.reply("✅ Тест: отправлено и закреплено.")

async def cmd_today(message: types.Message):
    d = get_today()
    await message.reply(render_text(d) + "\n\n" + format_schedule(d))

async def cmd_tomorrow(message: types.Message):
    d = next_workday(get_today())
    await message.reply(render_text(d) + "\n\n" + format_schedule(d))

async def cmd_schedule(message: types.Message):
    args = message.text.split()
    if len(args) > 1:
        try:
            d = datetime.strptime(args[1], "%Y-%m-%d").date()
        except ValueError:
            await message.reply("❌ Формат: /schedule YYYY-MM-DD")
            return
    else:
        d = get_today()
    await message.reply(format_schedule(d))

async def cmd_schedule_set(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    # /schedule_set пн математика | русский | физика
    txt = message.text[len("/schedule_set"):].strip()
    if not txt:
        await message.reply("❌ Использование: /schedule_set <день недели> предмет1 | предмет2 | ...")
        return
    parts = txt.split(None, 1)
    if len(parts) < 2:
        await message.reply("❌ Укажи день недели и список предметов через |")
        return
    day_raw, subjects_raw = parts[0], parts[1]
    subjects = [s.strip() for s in subjects_raw.split("|") if s.strip()]
    try:
        set_weekday_schedule(day_raw.lower(), subjects)
    except ValueError:
        await message.reply("❌ День недели: пн/вт/ср/чт/пт/сб/вс (или mon..sun)")
        return
    await message.reply("✅ Расписание на день обновлено.")

async def cmd_who(message: types.Message):
    args = message.text.split()
    if len(args) > 1:
        try:
            d = datetime.strptime(args[1], "%Y-%m-%d").date()
        except ValueError:
            await message.reply("❌ Формат: YYYY-MM-DD")
            return
    else:
        d = get_today()
    await message.reply(render_text(d))

async def cmd_send(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    args = message.text.split()
    if len(args) < 2:
        await message.reply("❌ Использование: /send YYYY-MM-DD")
        return
    try:
        d = datetime.strptime(args[1], "%Y-%m-%d").date()
    except ValueError:
        await message.reply("❌ Формат: YYYY-MM-DD")
        return
    await send_and_pin(message.bot, d)
    await message.reply(f"Отправлено за {fmt_ddmmyyyy(d)}.")

async def cmd_next(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    sim = load_sim_date() or get_today()
    sim = next_workday(sim)
    save_sim_date(sim)
    await send_and_pin(message.bot, sim)
    await message.reply(f"⏭ День → {fmt_ddmmyyyy(sim)}")

async def cmd_prev(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    sim = load_sim_date() or get_today()
    sim = prev_workday(sim)
    save_sim_date(sim)
    await send_and_pin(message.bot, sim)
    await message.reply(f"⏮ День → {fmt_ddmmyyyy(sim)}")

async def cmd_skip(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].lstrip("-").isdigit():
        await message.reply("❌ Использование: /skip N")
        return
    n = int(args[1])
    global START_DATE
    # сдвигаем старт на N рабочих дней (Пн–Сб)
    d = START_DATE
    step = -1 if n > 0 else 1
    n_abs = abs(n)
    while n_abs > 0:
        d = d + timedelta(days=step)
        if not is_sunday(d):
            n_abs -= 1
    START_DATE = d
    save_start_date(START_DATE)
    await message.reply(f"Очередь сдвинута на {n} рабочих дней.")

async def cmd_reset_all(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    global START_DATE, exceptions, debtors
    START_DATE = get_today()
    save_start_date(START_DATE)
    exceptions = {}
    debtors = []
    save_json(EXCEPTIONS_FILE, exceptions)
    save_json(DEBTORS_FILE, debtors)
    save_sim_date(None)
    await message.reply("бамбум.")

async def cmd_debtors(message: types.Message):
    if not debtors:
        await message.reply("✅ Должников нет.")
    else:
        await message.reply("Должники:\n" + "\n".join(f"- {idx_to_name(i)}" for i in debtors))

async def cmd_come(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    args = message.text.split()
    if len(args) > 1:
        try:
            target_date = datetime.strptime(args[1], "%Y-%m-%d").date()
        except ValueError:
            await message.reply("❌ Формат: YYYY-MM-DD")
            return
    else:
        target_date = get_today()
    if not debtors:
        await message.reply("Список должников пуст.")
        return
    kb = InlineKeyboardBuilder()
    dstr = target_date.strftime("%Y%m%d")
    for i in debtors:
        kb.button(text=idx_to_name(i), callback_data=f"come:{i}:{dstr}")
    kb.button(text="КАЗИНО", callback_data=f"come:random:{dstr}")
    await message.reply(f"Дата для отработки: {fmt_ddmmyyyy(target_date)}\nВыбери должника:", reply_markup=kb.as_markup())

async def on_come(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("ПОШЕЛ ВОН", show_alert=True)
        return
    _, payload, dstr = callback.data.split(":")
    target_date = datetime.strptime(dstr, "%Y%m%d").date()
    pair = get_pair(target_date)
    if payload == "random":
        if not debtors:
            await callback.answer("Список должников пуст.")
            return
        debtor_idx = random.choice(debtors)
    else:
        debtor_idx = int(payload)
    debtor_name = idx_to_name(debtor_idx)
    kb = InlineKeyboardBuilder()
    i0 = name_to_idx(pair[0])
    i1 = name_to_idx(pair[1])
    kb.button(text=f"↔ Заменить {pair[0]}", callback_data=f"replace:{debtor_idx}:{i0}:{dstr}")
    kb.button(text=f"↔ Заменить {pair[1]}", callback_data=f"replace:{debtor_idx}:{i1}:{dstr}")
    kb.button(text="КАЗИНО", callback_data=f"replace:{debtor_idx}:random:{dstr}")
    await callback.message.reply(
        f"Выбран должник: {debtor_name}\nКого заменить {fmt_ddmmyyyy(target_date)}?",
        reply_markup=kb.as_markup()
    )
    await callback.answer()

async def on_replace(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("ПОШЕЛ ВОН", show_alert=True)
        return
    _, debtor_idx_s, target_idx_s, dstr = callback.data.split(":")
    debtor_idx = int(debtor_idx_s)
    act_date = datetime.strptime(dstr, "%Y%m%d").date()
    pair = get_pair(act_date)
    if target_idx_s == "random":
        target_name = random.choice(pair)
    else:
        target_name = idx_to_name(int(target_idx_s))
    debtor_name = idx_to_name(debtor_idx)
    new_pair = [debtor_name if x == target_name else x for x in pair]
    set_exception(act_date, new_pair)
    pop_debtor_idx(debtor_idx)
    carry_over_person_to_next_day(target_name, act_date)
    await send_and_pin(callback.bot, act_date)
    await callback.message.answer(f"Должник {debtor_name} заменил {target_name} ({fmt_ddmmyyyy(act_date)}).")
    await callback.answer()

async def cmd_say(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    text = message.text[len("/say"):].strip()
    if not text:
        await message.reply("❌ Использование: /say текст")
        return
    await message.bot.send_message(GROUP_ID, text)
    await message.reply("✅ Отправлено.")

async def cmd_reload_students(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    global DUTY_LIST
    DUTY_LIST = load_students()
    await message.reply(f"🔁 Перечитал students.txt. Всего: {len(DUTY_LIST)}")

# seed / seed_only с гибким распознаванием имён
DATE_AT_END_RE = re.compile(r"\s(\d{4}-\d{2}-\d{2})$")
def _parse_seed_args(argstr: str) -> tuple[str, str, date]:
    m = DATE_AT_END_RE.search(argstr)
    if m:
        d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        names_part = argstr[:m.start()].strip()
    else:
        d = get_today()
        names_part = argstr.strip()
    if ";" not in names_part:
        raise ValueError("Нужно указать пары как ФИО1;ФИО2")
    raw1, raw2 = [x.strip() for x in names_part.split(";", 1)]
    n1 = try_resolve_name(raw1) or raw1
    n2 = try_resolve_name(raw2) or raw2
    # финальная проверка
    _ = name_to_idx(n1)
    _ = name_to_idx(n2)
    return n1, n2, d

def back_workdays(d: date, k: int) -> date:
    t = d
    for _ in range(k):
        t = prev_workday(t)
    return t

async def cmd_seed(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("❌ Использование: /seed ФИО1;ФИО2 [YYYY-MM-DD]")
        return
    try:
        name1, name2, D = _parse_seed_args(args[1])
    except Exception as e:
        await message.reply(f"❌ {e}")
        return
    i1, i2 = name_to_idx(name1), name_to_idx(name2)
    if not (i2 == i1 + 1 and i1 % 2 == 0):
        await message.reply("❌ Для /seed нужна смежная пара в порядке списка: (1-2), (3-4), ...\nЕсли разово — используй /seed_only.")
        return
    pair_index = i1 // 2
    new_start = back_workdays(D, pair_index)
    global START_DATE
    START_DATE = new_start
    save_start_date(START_DATE)
    key = fmt_ymd(D)
    if key in exceptions:
        del exceptions[key]
        save_json(EXCEPTIONS_FILE, exceptions)
    await send_and_pin(message.bot, D)
    await message.reply(f"✅ Сидирование на {fmt_ddmmyyyy(D)}.\nSTART_DATE → {START_DATE.isoformat()}")

async def cmd_seed_only(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply("❌ Использование: /seed_only ФИО1;ФИО2 [YYYY-MM-DD]")
        return
    try:
        n1, n2, D = _parse_seed_args(args[1])
    except Exception as e:
        await message.reply(f"❌ {e}")
        return
    set_exception(D, [n1, n2])
    await send_and_pin(message.bot, D)
    await message.reply(f"✅ Разовая фиксация пары на {fmt_ddmmyyyy(D)} сделана.")

# =====================
#       ЗАПУСК
# =====================
async def main():
    dp = Dispatcher()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    # команды
    dp.message.register(cmd_test,          Command("test"))
    dp.message.register(cmd_today,         Command("today"))
    dp.message.register(cmd_tomorrow,      Command("tomorrow"))
    dp.message.register(cmd_schedule,      Command("schedule"))
    dp.message.register(cmd_schedule_set,  Command("schedule_set"))
    dp.message.register(cmd_who,           Command("who"))
    dp.message.register(cmd_send,          Command("send"))
    dp.message.register(cmd_next,          Command("next"))
    dp.message.register(cmd_prev,          Command("prev"))
    dp.message.register(cmd_skip,          Command("skip"))
    dp.message.register(cmd_reset_all,     Command("reset_all"))
    dp.message.register(cmd_debtors,       Command("debtors"))
    dp.message.register(cmd_come,          Command("come"))
    dp.message.register(cmd_reload_students, Command("reload_students"))
    dp.message.register(cmd_seed,          Command("seed"))
    dp.message.register(cmd_seed_only,     Command("seed_only"))
    dp.message.register(cmd_say,           Command("say"))

    # коллбеки
    dp.callback_query.register(on_callback, lambda c: c.data.startswith(("ok:", "no:", "wipe:")))
    dp.callback_query.register(on_come,     lambda c: c.data.startswith("come:"))
    dp.callback_query.register(on_replace,  lambda c: c.data.startswith("replace:"))

    # расписание: Пн–Сб 00:00 (реальная дата)
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(
        send_and_pin,
        trigger=CronTrigger(day_of_week="mon-sat", hour=0, minute=0),
        args=[bot],
        id="duty-daily",
        replace_existing=True,
    )
    scheduler.start()

    print("✅ DutyBot 2.0 запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

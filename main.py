import asyncio
import os
import calendar
import asyncpg
import logging
import pytz
from datetime import datetime, timedelta, date
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BotCommand
)
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from prayer_api import fetch_prayer_times

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

bot = Bot(token=os.getenv('BOT_TOKEN'), default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

PRAYERS = ('Fajr', 'Dhuhr', 'Asr', 'Maghrib', 'Isha')
OFFSET_OPTIONS = (5, 10, 15, 20, 30)
NAFL_PRAYERS = ('Tahajjud', 'Duha', 'Ishraq', 'Awwabin', 'Tarawih')
QAZA_PRAYERS = ('Fajr', 'Dhuhr', 'Asr', 'Maghrib', 'Isha', 'Witr')
SUMMARY_HOUR = 21
calendar.setfirstweekday(calendar.MONDAY)

TIME_FMT = "%H:%M"

def fmt_dt(dt) -> str:
    return dt.strftime(TIME_FMT)

def fmt_time(hhmm: str) -> str:
    return datetime.strptime(hhmm.split()[0], "%H:%M").strftime(TIME_FMT)


def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="Log Prayers"), KeyboardButton(text="Nafl")],
        [KeyboardButton(text="Qaza"), KeyboardButton(text="View Schedule")],
        [KeyboardButton(text="Reports"), KeyboardButton(text="30-Day Overview")],
        [KeyboardButton(text="Profile"), KeyboardButton(text="Settings")],
        [KeyboardButton(text="Change Location")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def share_location_keyboard() -> ReplyKeyboardMarkup:
    kb = [[KeyboardButton(text="Share Location", request_location=True)]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True)


def report_selector() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Today", callback_data="report_day"),
        InlineKeyboardButton(text="7 Days", callback_data="report_week"),
        InlineKeyboardButton(text="30 Days", callback_data="report_month"),
    ]])


def generate_log_keyboard(target_date: date, completed: set[str]) -> InlineKeyboardMarkup:
    date_str = target_date.strftime("%Y-%m-%d")
    prev_date = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")
    next_date = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")

    def label(name: str) -> str:
        return f"✅ {name}" if name in completed else f"▫️ {name}"

    def btn(name: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=label(name), callback_data=f"pray_{name}_{date_str}")

    kb = [
        [btn("Fajr"), btn("Dhuhr")],
        [btn("Asr"), btn("Maghrib")],
        [btn("Isha")],
        [
            InlineKeyboardButton(text="‹ Prev", callback_data=f"nav_log_{prev_date}"),
            InlineKeyboardButton(text="Today", callback_data="log_today"),
            InlineKeyboardButton(text="Next ›", callback_data=f"nav_log_{next_date}"),
        ],
        [InlineKeyboardButton(text="📅 Pick a date", callback_data=f"cal_open_{date_str}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def generate_calendar(year: int, month: int, user_today: date) -> InlineKeyboardMarkup:
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    header = f"{calendar.month_name[month]} {year}"

    rows = [[
        InlineKeyboardButton(text="‹", callback_data=f"cal_nav_{prev_y}_{prev_m}"),
        InlineKeyboardButton(text=header, callback_data="ignore"),
        InlineKeyboardButton(text="›", callback_data=f"cal_nav_{next_y}_{next_m}"),
    ]]
    rows.append([InlineKeyboardButton(text=d, callback_data="ignore")
                 for d in ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")])

    for week in calendar.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
                continue
            this_day = date(year, month, day)
            if this_day > user_today:
                row.append(InlineKeyboardButton(text=str(day), callback_data="ignore"))
            else:
                row.append(InlineKeyboardButton(
                    text=str(day),
                    callback_data=f"cal_day_{this_day.strftime('%Y-%m-%d')}"
                ))
        rows.append(row)

    rows.append([InlineKeyboardButton(text="‹ Back to today", callback_data="log_today")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_keyboard(offset: int, asr_school: int) -> InlineKeyboardMarkup:
    def off_label(n: int) -> str:
        return f"✅ {n}m" if n == offset else f"{n}m"

    def asr_label(school: int, name: str) -> str:
        return f"✅ {name}" if school == asr_school else name

    rows = [
        [InlineKeyboardButton(text="⏰ Reminder lead time", callback_data="ignore")],
        [InlineKeyboardButton(text=off_label(n), callback_data=f"set_offset_{n}") for n in OFFSET_OPTIONS],
        [InlineKeyboardButton(text="🕌 Asr calculation", callback_data="ignore")],
        [
            InlineKeyboardButton(text=asr_label(1, "Hanafi"), callback_data="set_asr_1"),
            InlineKeyboardButton(text=asr_label(0, "Standard (Shafi'i)"), callback_data="set_asr_0"),
        ],
        [InlineKeyboardButton(text="— Danger zone —", callback_data="ignore")],
        [InlineKeyboardButton(text="🗑 Reset prayer logs", callback_data="danger_reset")],
        [InlineKeyboardButton(text="❌ Delete my data", callback_data="danger_delete")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def get_user_settings(db_pool: asyncpg.Pool, user_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT timezone, "
            "COALESCE(reminder_offset_mins, 15) AS reminder_offset_mins, "
            "COALESCE(asr_school, 1) AS asr_school "
            "FROM users WHERE user_id = $1",
            user_id
        )


async def get_user_timezone(db_pool: asyncpg.Pool, user_id: int) -> str | None:
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT timezone FROM users WHERE user_id = $1", user_id)


def user_local_date(tz_str: str | None) -> date:
    tz = pytz.timezone(tz_str) if tz_str else pytz.UTC
    return datetime.now(tz).date()


async def get_completed_prayers(db_pool: asyncpg.Pool, user_id: int, target_date: date) -> set[str]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT prayer_name FROM prayer_logs "
            "WHERE user_id = $1 AND prayer_date = $2 AND is_completed = TRUE AND category = 'fard'",
            user_id, target_date
        )
    return {r['prayer_name'] for r in rows}


async def get_completion_map(db_pool: asyncpg.Pool, user_id: int, start_date: date) -> dict:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT prayer_date, COUNT(DISTINCT prayer_name) AS cnt FROM prayer_logs "
            "WHERE user_id = $1 AND is_completed = TRUE AND prayer_date >= $2 AND category = 'fard' "
            "GROUP BY prayer_date",
            user_id, start_date
        )
    return {r['prayer_date']: r['cnt'] for r in rows}


def compute_streaks(complete_dates: list, today: date) -> tuple:
    date_set = set(complete_dates)
    if not date_set:
        return 0, 0

    current = 0
    cursor = today if today in date_set else today - timedelta(days=1)
    while cursor in date_set:
        current += 1
        cursor -= timedelta(days=1)

    longest = 0
    run = 0
    prev = None
    for d in sorted(date_set):
        run = run + 1 if (prev is not None and d == prev + timedelta(days=1)) else 1
        longest = max(longest, run)
        prev = d

    return current, longest


async def build_daily_report(db_pool: asyncpg.Pool, user_id: int, target_date: date) -> str:
    completed = await get_completed_prayers(db_pool, user_id, target_date)
    lines = [f"{'✅' if p in completed else '❌'} {p}" for p in PRAYERS]
    count = len(completed)
    footer = f"<b>{count}/5 prayers completed.</b>"
    if count == 5:
        footer += " 🎉"
    return f"<b>📅 Daily Report — {target_date}</b>\n\n" + "\n".join(lines) + f"\n\n{footer}"


async def build_range_report(db_pool, user_id, start_date, end_date, title) -> str:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT prayer_name, COUNT(*) AS cnt FROM prayer_logs "
            "WHERE user_id = $1 AND is_completed = TRUE "
            "AND category = 'fard' AND prayer_date BETWEEN $2 AND $3 GROUP BY prayer_name",
            user_id, start_date, end_date
        )
        first_log = await conn.fetchval(
            "SELECT MIN(prayer_date) FROM prayer_logs WHERE user_id = $1 AND category = 'fard'", user_id
        )
    per = {r['prayer_name']: r['cnt'] for r in rows}
    cmap = await get_completion_map(db_pool, user_id, start_date)

    effective_start = max(start_date, first_log) if first_log else end_date
    num_days = (end_date - effective_start).days + 1
    total = sum(per.values())
    possible = num_days * 5
    rate = round(total / possible * 100) if possible else 0
    full_days = sum(1 for d, c in cmap.items() if c == 5 and effective_start <= d <= end_date)

    prayer_lines = [f"{p}: {per.get(p, 0)}/{num_days}" for p in PRAYERS]

    text = (
        f"<b>{title}</b>\n{effective_start} → {end_date} "
        f"({num_days} {'day' if num_days == 1 else 'days'} tracked)\n\n"
        f"<b>Total:</b> {total}/{possible} ({rate}%)\n"
        f"<b>Perfect days:</b> {full_days}/{num_days}\n\n"
        f"<b>By prayer:</b>\n" + "\n".join(prayer_lines)
    )

    if total == 0:
        return text + "\n\nNo prayers logged in this period yet."

    missed = {p: num_days - per.get(p, 0) for p in PRAYERS}
    max_missed = max(missed.values())
    if max_missed > 0:
        worst = [p for p in PRAYERS if missed[p] == max_missed]
        text += (f"\n\n💡 Most missed: <b>{', '.join(worst)}</b> "
                 f"({max_missed} {'time' if max_missed == 1 else 'times'})")
    else:
        text += "\n\n🎉 No missed prayers — perfect period!"
    return text


def _square(count: int) -> str:
    return {
        5: "🟩",
        4: "🟨",
        3: "🟧",
        2: "🟥",
        1: "🟫",
    }.get(count, "⬜")


async def build_overview(db_pool: asyncpg.Pool, user_id: int, tz_str: str | None) -> str:
    today = user_local_date(tz_str)
    window_start = today - timedelta(days=29)
    grid_start = window_start - timedelta(days=window_start.weekday())
    cmap = await get_completion_map(db_pool, user_id, grid_start)
    async with db_pool.acquire() as conn:
        first_log = await conn.fetchval(
            "SELECT MIN(prayer_date) FROM prayer_logs WHERE user_id = $1 AND category = 'fard'", user_id
        )

    tracked_start = max(window_start, first_log) if first_log else today
    tracked_days = (today - tracked_start).days + 1

    cells = []
    cur = grid_start
    while cur <= today:
        cells.append(cur)
        cur += timedelta(days=1)
    while len(cells) % 7 != 0:
        cells.append(None)

    def cell(day):
        if day is None or day < tracked_start:
            return "⬛"
        return _square(cmap.get(day, 0))

    grid = "\n".join("".join(cell(d) for d in cells[i:i + 7]) for i in range(0, len(cells), 7))

    perfect = sum(1 for d, c in cmap.items() if c == 5 and d >= tracked_start)
    total = sum(c for d, c in cmap.items() if d >= tracked_start)
    rate = round(total / (tracked_days * 5) * 100) if tracked_days else 0
    complete_dates = [d for d, c in cmap.items() if c == 5]
    current_streak, _ = compute_streaks(complete_dates, today)

    return (
        f"<b>📊 Last 30 Days</b>\n\n{grid}\n\n"
        "🟩 5  🟨 4  🟧 3  🟥 2  🟫 1  ⬜ 0  ⬛ outside\n\n"
        f"<b>Perfect days:</b> {perfect}/{tracked_days}\n"
        f"<b>Completion:</b> {rate}%\n"
        f"🔥 <b>Current streak:</b> {current_streak} {'day' if current_streak == 1 else 'days'}"
    )


def generate_nafl_keyboard(target_date: date, completed: set[str]) -> InlineKeyboardMarkup:
    date_str = target_date.strftime("%Y-%m-%d")
    prev_date = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")
    next_date = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")

    def btn(name: str) -> InlineKeyboardButton:
        label = f"\u2705 {name}" if name in completed else f"\u25ab\ufe0f {name}"
        return InlineKeyboardButton(text=label, callback_data=f"nafl_set_{name}_{date_str}")

    rows = [[btn(NAFL_PRAYERS[i]), btn(NAFL_PRAYERS[i + 1])]
            if i + 1 < len(NAFL_PRAYERS) else [btn(NAFL_PRAYERS[i])]
            for i in range(0, len(NAFL_PRAYERS), 2)]
    rows.append([
        InlineKeyboardButton(text="\u2039 Prev", callback_data=f"nafl_nav_{prev_date}"),
        InlineKeyboardButton(text="Today", callback_data="nafl_today"),
        InlineKeyboardButton(text="Next \u203a", callback_data=f"nafl_nav_{next_date}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def generate_qaza_main(balances: dict) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{p} \u2014 {balances[p]}", callback_data=f"qaza_open_{p}")]
            for p in QAZA_PRAYERS]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def generate_qaza_adjuster(name: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="\u221210", callback_data=f"qaza_adj_{name}_-10"),
         InlineKeyboardButton(text="\u22125", callback_data=f"qaza_adj_{name}_-5"),
         InlineKeyboardButton(text="\u22121", callback_data=f"qaza_adj_{name}_-1")],
        [InlineKeyboardButton(text="+1", callback_data=f"qaza_adj_{name}_1"),
         InlineKeyboardButton(text="+10", callback_data=f"qaza_adj_{name}_10"),
         InlineKeyboardButton(text="+50", callback_data=f"qaza_adj_{name}_50")],
        [InlineKeyboardButton(text="\u2039 Back", callback_data="qaza_home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def get_completed_nafl(db_pool: asyncpg.Pool, user_id: int, target_date: date) -> set[str]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT prayer_name FROM prayer_logs "
            "WHERE user_id = $1 AND prayer_date = $2 AND is_completed = TRUE AND category = 'nafl'",
            user_id, target_date
        )
    return {r['prayer_name'] for r in rows}


async def get_qaza(db_pool: asyncpg.Pool, user_id: int) -> dict:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT prayer_name, remaining FROM qaza WHERE user_id = $1", user_id)
    found = {r['prayer_name']: r['remaining'] for r in rows}
    return {p: found.get(p, 0) for p in QAZA_PRAYERS}


async def send_prayer_reminder(bot: Bot, user_id: int, prayer_name: str, is_exact: bool):
    try:
        if is_exact:
            text = f"<b>It is time for {prayer_name}.</b>"
            kb = [[InlineKeyboardButton(text="Mark as Prayed", callback_data=f"pray_{prayer_name}")]]
            await bot.send_message(chat_id=user_id, text=text,
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        else:
            text = f"<b>{prayer_name} is approaching!</b>\nTake a moment to prepare and make Wudu."
            await bot.send_message(chat_id=user_id, text=text)
    except Exception as e:
        logging.error(f"Failed to send reminder to {user_id}: {e}")


async def send_fajr_end_reminder(bot: Bot, user_id: int, mins: int, ended: bool):
    try:
        if ended:
            text = "🌅 <b>Fajr time has ended.</b>\nThe sun has risen — Fajr is over for today."
        else:
            text = (f"⏳ <b>Fajr ends in {mins} minutes.</b>\n"
                    "If you haven't prayed Fajr yet, hurry before sunrise.")
        await bot.send_message(chat_id=user_id, text=text)
    except Exception as e:
        logging.error(f"Failed to send Fajr-end reminder to {user_id}: {e}")


async def send_summary_report(bot: Bot, db_pool: asyncpg.Pool, user_id: int, tz_str: str):
    today = user_local_date(tz_str)
    try:
        parts = [await build_daily_report(db_pool, user_id, today)]
        if today.weekday() == 6:
            parts.append(await build_range_report(
                db_pool, user_id, today - timedelta(days=6), today, "📆 Weekly Report"))
        if (today + timedelta(days=1)).month != today.month:
            parts.append(await build_range_report(
                db_pool, user_id, today - timedelta(days=29), today, "🗓 Monthly Report (last 30 days)"))
        await bot.send_message(user_id, "🌙 <b>End-of-day summary</b>\n\n" + "\n\n".join(parts))
    except Exception as e:
        logging.error(f"Summary failed for {user_id}: {e}")


async def daily_scheduler_job(db_pool: asyncpg.Pool, bot: Bot, scheduler: AsyncIOScheduler):
    logging.info("Running background master job to queue today's prayer times...")

    for job in scheduler.get_jobs():
        if job.id != "daily_master_job":
            job.remove()

    async with db_pool.acquire() as conn:
        users = await conn.fetch(
            "SELECT user_id, latitude, longitude, timezone, "
            "COALESCE(reminder_offset_mins, 15) AS reminder_offset_mins, "
            "COALESCE(asr_school, 1) AS asr_school "
            "FROM users WHERE latitude IS NOT NULL"
        )

    queued = 0
    for user in users:
        prayer_data = await fetch_prayer_times(user['latitude'], user['longitude'], school=user['asr_school'])
        if not prayer_data:
            continue

        user_tz = pytz.timezone(user['timezone'])
        now = datetime.now(user_tz)

        for prayer_name, time_str in prayer_data['timings'].items():
            hour, minute = map(int, time_str.split(':'))
            event_dt = user_tz.localize(datetime(now.year, now.month, now.day, hour, minute))

            if prayer_name == "Sunrise":
                warn_dt = event_dt - timedelta(minutes=user['reminder_offset_mins'])
                if warn_dt > now:
                    scheduler.add_job(send_fajr_end_reminder, 'date', run_date=warn_dt,
                                      id=f"reminder_{user['user_id']}_Sunrise_warning",
                                      args=[bot, user['user_id'], user['reminder_offset_mins'], False])
                    queued += 1
                if event_dt > now:
                    scheduler.add_job(send_fajr_end_reminder, 'date', run_date=event_dt,
                                      id=f"reminder_{user['user_id']}_Sunrise_exact",
                                      args=[bot, user['user_id'], user['reminder_offset_mins'], True])
                    queued += 1
                continue

            if prayer_name not in PRAYERS:
                continue

            adhan_dt = event_dt
            reminder_dt = adhan_dt - timedelta(minutes=user['reminder_offset_mins'])

            if reminder_dt > now:
                scheduler.add_job(send_prayer_reminder, 'date', run_date=reminder_dt,
                                  id=f"reminder_{user['user_id']}_{prayer_name}_warning",
                                  args=[bot, user['user_id'], prayer_name, False])
                queued += 1
            if adhan_dt > now:
                scheduler.add_job(send_prayer_reminder, 'date', run_date=adhan_dt,
                                  id=f"reminder_{user['user_id']}_{prayer_name}_exact",
                                  args=[bot, user['user_id'], prayer_name, True])
                queued += 1

        summary_dt = user_tz.localize(datetime(now.year, now.month, now.day, SUMMARY_HOUR, 0))
        if summary_dt > now:
            scheduler.add_job(send_summary_report, 'date', run_date=summary_dt,
                              id=f"summary_{user['user_id']}",
                              args=[bot, db_pool, user['user_id'], user['timezone']])
            queued += 1

    logging.info(f"Successfully queued {queued} upcoming jobs for today.")


@dp.callback_query(F.data == "settings_home")
async def settings_home_handler(callback: CallbackQuery, db_pool: asyncpg.Pool) -> None:
    s = await get_user_settings(db_pool, callback.from_user.id)
    await callback.message.edit_text(
        "<b>⚙️ Settings</b>\nChoose how early you want reminders and your Asr method.",
        reply_markup=settings_keyboard(s['reminder_offset_mins'], s['asr_school']),
    )
    await callback.answer()


@dp.callback_query(F.data == "danger_reset")
async def danger_reset_handler(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "⚠️ <b>Reset prayer logs?</b>\n\nThis deletes <b>all</b> your logged prayers. "
        "Your location and settings are kept. This cannot be undone.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Yes, reset", callback_data="danger_reset_confirm"),
            InlineKeyboardButton(text="Cancel", callback_data="settings_home"),
        ]]),
    )
    await callback.answer()


@dp.callback_query(F.data == "danger_reset_confirm")
async def danger_reset_confirm_handler(callback: CallbackQuery, db_pool: asyncpg.Pool) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM prayer_logs WHERE user_id = $1", callback.from_user.id)
    await callback.message.edit_text("🗑 Your prayer logs have been reset.")
    await callback.answer("Prayer logs reset")


@dp.callback_query(F.data == "danger_delete")
async def danger_delete_handler(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "⚠️ <b>Delete all your data?</b>\n\nThis removes your account, location, settings, "
        "and every logged prayer, and stops all reminders. This cannot be undone.\n\n"
        "You can re-register anytime with /start.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Yes, delete everything", callback_data="danger_delete_confirm"),
            InlineKeyboardButton(text="Cancel", callback_data="settings_home"),
        ]]),
    )
    await callback.answer()


@dp.callback_query(F.data == "danger_delete_confirm")
async def danger_delete_confirm_handler(callback: CallbackQuery, db_pool: asyncpg.Pool, scheduler: AsyncIOScheduler) -> None:
    user_id = callback.from_user.id
    for job in scheduler.get_jobs():
        if job.id.startswith(f"reminder_{user_id}_") or job.id == f"summary_{user_id}":
            job.remove()
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)
    await callback.message.edit_text("✅ All your data has been deleted.")
    await callback.message.answer(
        "Send /start whenever you'd like to register again.",
        reply_markup=ReplyKeyboardRemove()
    )
    await callback.answer("Data deleted")


@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    await message.answer(
        f"Assalamu alaykum, {message.from_user.full_name}!\n\n"
        "To track your prayers and send accurate daily reminders, I need to know your local timezone.\n"
        "Please press the button below to share your location.\n\n"
        "🔔 Make sure notifications are <b>on</b> for this chat so you don't miss any prayer alerts.",
        reply_markup=share_location_keyboard()
    )


@dp.message(Command("change_location"))
@dp.message(F.text == "Change Location")
async def change_location_handler(message: Message) -> None:
    await message.answer(
        "Send your new location to update your timezone and prayer times. "
        "Your settings and logged prayers are kept.",
        reply_markup=share_location_keyboard()
    )


@dp.message(Command("test"))
async def test_handler(message: Message) -> None:
    await message.answer(
        "🔔 <b>Test notification.</b>\nIf this arrived as a notification, your alerts are working. "
        "If it was silent, enable notifications for this chat in Telegram's settings."
    )


@dp.message(Command("help"))
async def help_handler(message: Message) -> None:
    text = (
        "<b>Halal Bot — Help</b>\n\n"
        "/start — share location and set up reminders\n"
        "/log_prayers — mark prayers, jump to any day or pick a date\n"
        "/nafl — log voluntary (nafl) prayers\n"
        "/qaza — track and pay down missed prayers\n"
        "/view_schedule — today's upcoming alerts\n"
        "/reports — today / 7-day / 30-day summaries\n"
        "/overview — your activity grid\n"
        "/profile — stats and streaks\n"
        "/settings — reminder lead time and Asr calculation\n"
        "/change_location — update your timezone and prayer times\n"
        "/test — send a test notification\n\n"
        "I send a heads-up before each prayer, an alert at the exact time, "
        "a Fajr-ending warning before sunrise, and an end-of-day summary."
    )
    await message.answer(text, reply_markup=get_main_menu_keyboard())


@dp.message(Command("view_schedule"))
@dp.message(F.text == "View Schedule")
async def check_schedule_handler(message: Message, scheduler: AsyncIOScheduler) -> None:
    user_id_str = str(message.from_user.id)
    user_jobs = []
    for job in scheduler.get_jobs():
        if not job.id.startswith("reminder_"):
            continue
        parts = job.id.split('_')
        if parts[1] != user_id_str:
            continue
        time_str = fmt_dt(job.next_run_time)
        if parts[2] == "Sunrise":
            if parts[3] == "warning":
                user_jobs.append(f"<b>Fajr ending soon:</b> {time_str}")
            else:
                user_jobs.append(f"<b>Fajr ends (sunrise):</b> {time_str}\n")
        elif parts[3] == "warning":
            user_jobs.append(f"<b>{parts[2]} Warning:</b> {time_str}")
        elif parts[3] == "exact":
            user_jobs.append(f"<b>{parts[2]} Adhan:</b> {time_str}\n")

    if not user_jobs:
        await message.answer("You have no upcoming prayer alerts scheduled for the rest of today.")
    else:
        await message.answer("<b>Your Upcoming Alerts Today:</b>\n\n" + "\n".join(user_jobs))


@dp.message(Command("profile"))
@dp.message(F.text == "Profile")
async def profile_handler(message: Message, db_pool: asyncpg.Pool) -> None:
    user_id = message.from_user.id
    tz_str = await get_user_timezone(db_pool, user_id)
    local_today = user_local_date(tz_str)

    stats_query = """
        SELECT COUNT(*) AS total_prayers,
               COUNT(*) FILTER (WHERE prayer_date = $2) AS today_prayers
        FROM prayer_logs WHERE user_id = $1 AND is_completed = TRUE AND category = 'fard';
    """
    streak_query = """
        SELECT prayer_date FROM prayer_logs
        WHERE user_id = $1 AND is_completed = TRUE AND category = 'fard'
        GROUP BY prayer_date HAVING COUNT(DISTINCT prayer_name) = 5
        ORDER BY prayer_date;
    """
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(stats_query, user_id, local_today)
        complete_rows = await conn.fetch(streak_query, user_id)

    total = row['total_prayers'] or 0
    today = row['today_prayers'] or 0
    current_streak, longest_streak = compute_streaks([r['prayer_date'] for r in complete_rows], local_today)

    streak_line = f"🔥 <b>Current Streak:</b> {current_streak} {'day' if current_streak == 1 else 'days'}"
    if current_streak == 0:
        streak_line += "  — complete all 5 today to start one!"

    await message.answer(
        f"<b>Profile: {message.from_user.full_name}</b>\n\n"
        f"{streak_line}\n"
        f"🏆 <b>Longest Streak:</b> {longest_streak} {'day' if longest_streak == 1 else 'days'}\n\n"
        f"<b>Prayers Logged Today:</b> {today}/5\n"
        f"<b>Lifetime Prayers Logged:</b> {total}\n\n"
        "Keep up the good work!"
    )


@dp.message(F.location)
async def location_handler(message: Message, db_pool: asyncpg.Pool, scheduler: AsyncIOScheduler) -> None:
    lat, lon = message.location.latitude, message.location.longitude
    user_id = message.from_user.id

    existing = await get_user_settings(db_pool, user_id)
    school = existing['asr_school'] if existing else 1

    processing_msg = await message.answer(
        "Calculating your timezone and prayer schedules...",
        reply_markup=ReplyKeyboardRemove()
    )
    prayer_data = await fetch_prayer_times(lat, lon, school=school)
    if not prayer_data:
        await processing_msg.delete()
        await message.answer("Failed to calculate your timezone. Please try sharing your location again later.")
        return

    user_timezone = prayer_data['timezone']
    query = """
        INSERT INTO users (user_id, username, full_name, timezone, latitude, longitude)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (user_id) DO UPDATE SET
            username = EXCLUDED.username, full_name = EXCLUDED.full_name,
            timezone = EXCLUDED.timezone, latitude = EXCLUDED.latitude,
            longitude = EXCLUDED.longitude;
    """
    async with db_pool.acquire() as conn:
        await conn.execute(query, user_id, message.from_user.username,
                           message.from_user.full_name, user_timezone, lat, lon)

    await daily_scheduler_job(db_pool, bot, scheduler)

    t = prayer_data['timings']
    timings_lines = "\n".join(
        f"{name}: {fmt_time(t[name])}"
        for name in ("Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha")
    )
    await processing_msg.delete()
    await message.answer(
        f"<b>Location registered successfully!</b>\n<b>Timezone:</b> {user_timezone}\n\n"
        f"<b>Today's Timings:</b>\n{timings_lines}\n\n"
        "I have automatically set up your prayer alerts!\n\n"
        "🔔 Make sure notifications are <b>on</b> for this chat so you don't miss any alerts.",
        reply_markup=get_main_menu_keyboard()
    )


@dp.message(Command("log_prayers"))
@dp.message(F.text == "Log Prayers")
async def log_manual_handler(message: Message, db_pool: asyncpg.Pool) -> None:
    tz_str = await get_user_timezone(db_pool, message.from_user.id)
    if not tz_str:
        await message.answer("Please set your location first using /start.")
        return
    today = user_local_date(tz_str)
    completed = await get_completed_prayers(db_pool, message.from_user.id, today)
    await message.answer(
        f"<b>Manual Logging for {today}</b>\nTap a prayer to toggle it. ✅ means logged.",
        reply_markup=generate_log_keyboard(today, completed),
    )


@dp.message(Command("reports"))
@dp.message(F.text == "Reports")
async def reports_handler(message: Message) -> None:
    await message.answer("📑 <b>Reports</b> — choose a period:", reply_markup=report_selector())


@dp.message(Command("overview"))
@dp.message(F.text == "30-Day Overview")
async def overview_handler(message: Message, db_pool: asyncpg.Pool) -> None:
    tz_str = await get_user_timezone(db_pool, message.from_user.id)
    if not tz_str:
        await message.answer("Please set your location first using /start.")
        return
    await message.answer(await build_overview(db_pool, message.from_user.id, tz_str))


@dp.message(Command("settings"))
@dp.message(F.text == "Settings")
async def settings_handler(message: Message, db_pool: asyncpg.Pool) -> None:
    s = await get_user_settings(db_pool, message.from_user.id)
    if not s:
        await message.answer("Please set your location first using /start.")
        return
    await message.answer(
        "<b>⚙️ Settings</b>\nChoose how early you want reminders and your Asr method.",
        reply_markup=settings_keyboard(s['reminder_offset_mins'], s['asr_school']),
    )


@dp.callback_query(F.data == "ignore")
async def ignore_callback(callback: CallbackQuery) -> None:
    await callback.answer()


@dp.callback_query(F.data == "log_today")
async def log_today_handler(callback: CallbackQuery, db_pool: asyncpg.Pool) -> None:
    tz_str = await get_user_timezone(db_pool, callback.from_user.id)
    today = user_local_date(tz_str)
    completed = await get_completed_prayers(db_pool, callback.from_user.id, today)
    await callback.message.edit_text(
        f"<b>Manual Logging for {today}</b>\nTap a prayer to toggle it. ✅ means logged.",
        reply_markup=generate_log_keyboard(today, completed),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("nav_log_"))
async def log_nav_handler(callback: CallbackQuery, db_pool: asyncpg.Pool) -> None:
    target_date = datetime.strptime(callback.data.split("_")[2], "%Y-%m-%d").date()
    completed = await get_completed_prayers(db_pool, callback.from_user.id, target_date)
    await callback.message.edit_text(
        f"<b>Manual Logging for {target_date}</b>\nTap a prayer to toggle it. ✅ means logged.",
        reply_markup=generate_log_keyboard(target_date, completed),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("cal_open_"))
async def cal_open_handler(callback: CallbackQuery, db_pool: asyncpg.Pool) -> None:
    anchor = datetime.strptime(callback.data[len("cal_open_"):], "%Y-%m-%d").date()
    tz_str = await get_user_timezone(db_pool, callback.from_user.id)
    user_today = user_local_date(tz_str)
    await callback.message.edit_text(
        "<b>📅 Pick a date to log</b>",
        reply_markup=generate_calendar(anchor.year, anchor.month, user_today),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("cal_nav_"))
async def cal_nav_handler(callback: CallbackQuery, db_pool: asyncpg.Pool) -> None:
    _, _, year, month = callback.data.split("_")
    tz_str = await get_user_timezone(db_pool, callback.from_user.id)
    user_today = user_local_date(tz_str)
    await callback.message.edit_text(
        "<b>📅 Pick a date to log</b>",
        reply_markup=generate_calendar(int(year), int(month), user_today),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("cal_day_"))
async def cal_day_handler(callback: CallbackQuery, db_pool: asyncpg.Pool) -> None:
    target_date = datetime.strptime(callback.data[len("cal_day_"):], "%Y-%m-%d").date()
    completed = await get_completed_prayers(db_pool, callback.from_user.id, target_date)
    await callback.message.edit_text(
        f"<b>Manual Logging for {target_date}</b>\nTap a prayer to toggle it. ✅ means logged.",
        reply_markup=generate_log_keyboard(target_date, completed),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("report_"))
async def report_handler(callback: CallbackQuery, db_pool: asyncpg.Pool) -> None:
    kind = callback.data.split("_")[1]
    tz_str = await get_user_timezone(db_pool, callback.from_user.id)
    today = user_local_date(tz_str)

    if kind == "day":
        text = await build_daily_report(db_pool, callback.from_user.id, today)
    elif kind == "week":
        text = await build_range_report(db_pool, callback.from_user.id,
                                         today - timedelta(days=6), today, "📆 Weekly Report")
    else:
        text = await build_range_report(db_pool, callback.from_user.id,
                                         today - timedelta(days=29), today, "🗓 30-Day Report")

    await callback.message.edit_text(text, reply_markup=report_selector())
    await callback.answer()


@dp.callback_query(F.data.startswith("set_offset_"))
async def set_offset_handler(callback: CallbackQuery, db_pool: asyncpg.Pool, scheduler: AsyncIOScheduler) -> None:
    offset = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET reminder_offset_mins = $1 WHERE user_id = $2",
                           offset, callback.from_user.id)
    await daily_scheduler_job(db_pool, bot, scheduler)
    s = await get_user_settings(db_pool, callback.from_user.id)
    await callback.message.edit_text(
        "<b>⚙️ Settings</b>\nChoose how early you want reminders and your Asr method.",
        reply_markup=settings_keyboard(s['reminder_offset_mins'], s['asr_school']),
    )
    await callback.answer(f"Reminder lead time set to {offset} minutes")


@dp.callback_query(F.data.startswith("set_asr_"))
async def set_asr_handler(callback: CallbackQuery, db_pool: asyncpg.Pool, scheduler: AsyncIOScheduler) -> None:
    school = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET asr_school = $1 WHERE user_id = $2",
                           school, callback.from_user.id)
    await daily_scheduler_job(db_pool, bot, scheduler)
    s = await get_user_settings(db_pool, callback.from_user.id)
    await callback.message.edit_text(
        "<b>⚙️ Settings</b>\nChoose how early you want reminders and your Asr method.",
        reply_markup=settings_keyboard(s['reminder_offset_mins'], s['asr_school']),
    )
    await callback.answer(f"Asr method set to {'Hanafi' if school == 1 else 'Standard'}")


@dp.callback_query(F.data.startswith("pray_"))
async def log_prayer_handler(callback: CallbackQuery, db_pool: asyncpg.Pool, scheduler: AsyncIOScheduler) -> None:
    parts = callback.data.split("_")
    prayer_name = parts[1]
    user_id = callback.from_user.id
    from_menu = len(parts) == 3

    tz_str = await get_user_timezone(db_pool, user_id)
    if not tz_str:
        await callback.answer("Please set your location first using /start", show_alert=True)
        return

    user_now = datetime.now(pytz.timezone(tz_str))
    target_date = datetime.strptime(parts[2], "%Y-%m-%d").date() if from_menu else user_now.date()

    if target_date > user_now.date():
        await callback.answer("You cannot log prayers for future dates.", show_alert=True)
        return

    async with db_pool.acquire() as conn:
        current = await conn.fetchval(
            "SELECT is_completed FROM prayer_logs "
            "WHERE user_id = $1 AND prayer_name = $2 AND prayer_date = $3 AND category = 'fard'",
            user_id, prayer_name, target_date
        )
    new_state = (not bool(current)) if from_menu else True

    if new_state and target_date == user_now.date():
        job = scheduler.get_job(f"reminder_{user_id}_{prayer_name}_exact")
        if job:
            await callback.answer(
                f"It is not time for {prayer_name} yet. Adhan is at {fmt_dt(job.next_run_time)}.",
                show_alert=True
            )
            return

    upsert = """
        INSERT INTO prayer_logs (user_id, prayer_name, prayer_date, is_completed, category)
        VALUES ($1, $2, $3, $4, 'fard')
        ON CONFLICT (user_id, prayer_name, prayer_date)
        DO UPDATE SET is_completed = $4;
    """
    async with db_pool.acquire() as conn:
        await conn.execute(upsert, user_id, prayer_name, target_date, new_state)

    if from_menu:
        completed = await get_completed_prayers(db_pool, user_id, target_date)
        await callback.message.edit_reply_markup(reply_markup=generate_log_keyboard(target_date, completed))
        await callback.answer(f"{prayer_name} {'logged ✅' if new_state else 'unmarked'}")
    else:
        done_kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✅ Prayed", callback_data="ignore")]]
        )
        await callback.message.edit_reply_markup(reply_markup=done_kb)
        await callback.answer(f"{prayer_name} logged ✅")


@dp.message(Command("nafl"))
@dp.message(F.text == "Nafl")
async def nafl_handler(message: Message, db_pool: asyncpg.Pool) -> None:
    tz_str = await get_user_timezone(db_pool, message.from_user.id)
    if not tz_str:
        await message.answer("Please set your location first using /start.")
        return
    today = user_local_date(tz_str)
    completed = await get_completed_nafl(db_pool, message.from_user.id, today)
    await message.answer(
        f"<b>\U0001f319 Nafl \u2014 {today}</b>\nTap to toggle. \u2705 means done.",
        reply_markup=generate_nafl_keyboard(today, completed),
    )


@dp.callback_query(F.data == "nafl_today")
async def nafl_today_handler(callback: CallbackQuery, db_pool: asyncpg.Pool) -> None:
    tz_str = await get_user_timezone(db_pool, callback.from_user.id)
    today = user_local_date(tz_str)
    completed = await get_completed_nafl(db_pool, callback.from_user.id, today)
    await callback.message.edit_text(
        f"<b>\U0001f319 Nafl \u2014 {today}</b>\nTap to toggle. \u2705 means done.",
        reply_markup=generate_nafl_keyboard(today, completed),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("nafl_nav_"))
async def nafl_nav_handler(callback: CallbackQuery, db_pool: asyncpg.Pool) -> None:
    target_date = datetime.strptime(callback.data[len("nafl_nav_"):], "%Y-%m-%d").date()
    completed = await get_completed_nafl(db_pool, callback.from_user.id, target_date)
    await callback.message.edit_text(
        f"<b>\U0001f319 Nafl \u2014 {target_date}</b>\nTap to toggle. \u2705 means done.",
        reply_markup=generate_nafl_keyboard(target_date, completed),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("nafl_set_"))
async def nafl_set_handler(callback: CallbackQuery, db_pool: asyncpg.Pool) -> None:
    name, date_str = callback.data[len("nafl_set_"):].rsplit("_", 1)
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    user_id = callback.from_user.id

    tz_str = await get_user_timezone(db_pool, user_id)
    if target_date > user_local_date(tz_str):
        await callback.answer("You cannot log future dates.", show_alert=True)
        return

    async with db_pool.acquire() as conn:
        current = await conn.fetchval(
            "SELECT is_completed FROM prayer_logs "
            "WHERE user_id = $1 AND prayer_name = $2 AND prayer_date = $3 AND category = 'nafl'",
            user_id, name, target_date
        )
        new_state = not bool(current)
        await conn.execute(
            "INSERT INTO prayer_logs (user_id, prayer_name, prayer_date, is_completed, category) "
            "VALUES ($1, $2, $3, $4, 'nafl') "
            "ON CONFLICT (user_id, prayer_name, prayer_date) DO UPDATE SET is_completed = $4",
            user_id, name, target_date, new_state
        )
    completed = await get_completed_nafl(db_pool, user_id, target_date)
    await callback.message.edit_reply_markup(reply_markup=generate_nafl_keyboard(target_date, completed))
    await callback.answer(f"{name} {'\u2705' if new_state else 'unmarked'}")


@dp.message(Command("qaza"))
@dp.message(F.text == "Qaza")
async def qaza_handler(message: Message, db_pool: asyncpg.Pool) -> None:
    balances = await get_qaza(db_pool, message.from_user.id)
    total = sum(balances.values())
    await message.answer(
        f"<b>\U0001f9ee Qaza \u2014 missed prayers</b>\nTotal remaining: <b>{total}</b>\n\n"
        "Tap a prayer to add to your count (+) or record make-ups (\u2212).",
        reply_markup=generate_qaza_main(balances),
    )


@dp.callback_query(F.data == "qaza_home")
async def qaza_home_handler(callback: CallbackQuery, db_pool: asyncpg.Pool) -> None:
    balances = await get_qaza(db_pool, callback.from_user.id)
    total = sum(balances.values())
    await callback.message.edit_text(
        f"<b>\U0001f9ee Qaza \u2014 missed prayers</b>\nTotal remaining: <b>{total}</b>\n\n"
        "Tap a prayer to add to your count (+) or record make-ups (\u2212).",
        reply_markup=generate_qaza_main(balances),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("qaza_open_"))
async def qaza_open_handler(callback: CallbackQuery, db_pool: asyncpg.Pool) -> None:
    name = callback.data[len("qaza_open_"):]
    balances = await get_qaza(db_pool, callback.from_user.id)
    await callback.message.edit_text(
        f"<b>{name} \u2014 {balances.get(name, 0)} remaining</b>\n\n"
        "\u2212 records make-ups, + adds to your count.",
        reply_markup=generate_qaza_adjuster(name),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("qaza_adj_"))
async def qaza_adj_handler(callback: CallbackQuery, db_pool: asyncpg.Pool) -> None:
    name, delta_str = callback.data[len("qaza_adj_"):].rsplit("_", 1)
    delta = int(delta_str)
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO qaza (user_id, prayer_name, remaining) VALUES ($1, $2, GREATEST(0, $3)) "
            "ON CONFLICT (user_id, prayer_name) DO UPDATE SET remaining = GREATEST(0, qaza.remaining + $3)",
            user_id, name, delta
        )
    balances = await get_qaza(db_pool, user_id)
    await callback.message.edit_text(
        f"<b>{name} \u2014 {balances.get(name, 0)} remaining</b>\n\n"
        "\u2212 records make-ups, + adds to your count.",
        reply_markup=generate_qaza_adjuster(name),
    )
    await callback.answer(f"{name}: {balances.get(name, 0)} remaining")


@dp.message()
async def fallback_handler(message: Message) -> None:
    await message.answer(
        "I didn't understand that. Use the menu buttons or /help.",
        reply_markup=get_main_menu_keyboard()
    )


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Set location & reminders"),
        BotCommand(command="log_prayers", description="Log prayers"),
        BotCommand(command="nafl", description="Log voluntary (nafl) prayers"),
        BotCommand(command="qaza", description="Track missed-prayer make-ups"),
        BotCommand(command="view_schedule", description="Today's upcoming alerts"),
        BotCommand(command="reports", description="Daily / weekly / monthly reports"),
        BotCommand(command="overview", description="30-day activity grid"),
        BotCommand(command="profile", description="Your stats & streaks"),
        BotCommand(command="settings", description="Reminder time & Asr method"),
        BotCommand(command="change_location", description="Update your location"),
        BotCommand(command="test", description="Send a test notification"),
        BotCommand(command="help", description="How the bot works"),
    ])


async def main() -> None:
    pool = await asyncpg.create_pool(
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        database=os.getenv('DB_NAME'),
        host=os.getenv('DB_HOST'),
        port=os.getenv('DB_PORT')
    )
    logging.info("Database connection pool created.")

    scheduler = AsyncIOScheduler()
    scheduler.add_job(daily_scheduler_job, 'cron', hour=0, minute=1,
                      id="daily_master_job", args=[pool, bot, scheduler])
    scheduler.start()

    await daily_scheduler_job(pool, bot, scheduler)
    await set_bot_commands(bot)

    dp.workflow_data.update({'db_pool': pool, 'scheduler': scheduler})

    try:
        logging.info("Bot is up and running...")
        await dp.start_polling(bot)
    finally:
        await pool.close()
        logging.info("Database connection closed.")


if __name__ == "__main__":
    asyncio.run(main())
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
SUMMARY_HOUR = 21
calendar.setfirstweekday(calendar.MONDAY)


def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="Profile"), KeyboardButton(text="Log Prayers")],
        [KeyboardButton(text="Reports"), KeyboardButton(text="30-Day Overview")],
        [KeyboardButton(text="View Schedule"), KeyboardButton(text="Settings")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def report_selector() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Today", callback_data="report_day"),
        InlineKeyboardButton(text="7 Days", callback_data="report_week"),
        InlineKeyboardButton(text="30 Days", callback_data="report_month"),
    ]])


def generate_log_keyboard(target_date: date, completed: set[str]) -> InlineKeyboardMarkup:
    """Log keyboard with ✅ on completed prayers, plus Today / Calendar nav."""
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
    """Month picker. Past/today are tappable; future days are inert."""
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
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def get_user_settings(db_pool: asyncpg.Pool, user_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT timezone, "
            "COALESCE(reminder_offset_mins, 5) AS reminder_offset_mins, "
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
            "WHERE user_id = $1 AND prayer_date = $2 AND is_completed = TRUE",
            user_id, target_date
        )
    return {r['prayer_name'] for r in rows}


async def get_completion_map(db_pool: asyncpg.Pool, user_id: int, start_date: date) -> dict:
    """date -> number of distinct prayers completed, for dates >= start_date."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT prayer_date, COUNT(DISTINCT prayer_name) AS cnt FROM prayer_logs "
            "WHERE user_id = $1 AND is_completed = TRUE AND prayer_date >= $2 "
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
            "AND prayer_date BETWEEN $2 AND $3 GROUP BY prayer_name",
            user_id, start_date, end_date
        )
    per = {r['prayer_name']: r['cnt'] for r in rows}
    cmap = await get_completion_map(db_pool, user_id, start_date)

    num_days = (end_date - start_date).days + 1
    total = sum(per.values())
    possible = num_days * 5
    rate = round(total / possible * 100) if possible else 0
    full_days = sum(1 for d, c in cmap.items() if c == 5 and start_date <= d <= end_date)

    prayer_lines = [f"{p}: {per.get(p, 0)}/{num_days}" for p in PRAYERS]

    text = (
        f"<b>{title}</b>\n{start_date} → {end_date}\n\n"
        f"<b>Total:</b> {total}/{possible} ({rate}%)\n"
        f"<b>Perfect days:</b> {full_days}/{num_days}\n\n"
        f"<b>By prayer:</b>\n" + "\n".join(prayer_lines)
    )
    if total > 0:
        worst = min(PRAYERS, key=lambda p: per.get(p, 0))
        text += f"\n\n💡 Most missed: <b>{worst}</b>"
    return text


def _square(count: int) -> str:
    if count >= 5:
        return "🟩"
    if count >= 3:
        return "🟨"
    if count >= 1:
        return "🟥"
    return "⬜"


async def build_overview(db_pool: asyncpg.Pool, user_id: int, tz_str: str | None) -> str:
    today = user_local_date(tz_str)
    window_start = today - timedelta(days=29)
    grid_start = window_start - timedelta(days=window_start.weekday())
    cmap = await get_completion_map(db_pool, user_id, grid_start)

    cells = []
    cur = grid_start
    while cur <= today:
        cells.append(cur)
        cur += timedelta(days=1)
    while len(cells) % 7 != 0:
        cells.append(None)

    def cell(day):
        if day is None or day < window_start:
            return "⬛"
        return _square(cmap.get(day, 0))

    grid = "\n".join("".join(cell(d) for d in cells[i:i + 7]) for i in range(0, len(cells), 7))

    perfect = sum(1 for d, c in cmap.items() if c == 5 and d >= window_start)
    total = sum(c for d, c in cmap.items() if d >= window_start)
    rate = round(total / (30 * 5) * 100)
    complete_dates = [d for d, c in cmap.items() if c == 5]
    current_streak, _ = compute_streaks(complete_dates, today)

    return (
        f"<b>📊 Last 30 Days</b>\n\n{grid}\n\n"
        "🟩 all 5  🟨 3-4  🟥 1-2  ⬜ none  ⬛ outside\n\n"
        f"<b>Perfect days:</b> {perfect}/30\n"
        f"<b>Completion:</b> {rate}%\n"
        f"🔥 <b>Current streak:</b> {current_streak} {'day' if current_streak == 1 else 'days'}"
    )

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
            "COALESCE(reminder_offset_mins, 5) AS reminder_offset_mins, "
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
            if prayer_name not in PRAYERS:
                continue
            hour, minute = map(int, time_str.split(':'))
            adhan_dt = user_tz.localize(datetime(now.year, now.month, now.day, hour, minute))
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

@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    kb = [[KeyboardButton(text="Share Location", request_location=True)]]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True)
    await message.answer(
        f"Assalamu alaykum, {message.from_user.full_name}!\n\n"
        "To track your prayers and send accurate daily reminders, I need to know your local timezone.\n"
        "Please press the button below to share your location.",
        reply_markup=keyboard
    )


@dp.message(Command("help"))
async def help_handler(message: Message) -> None:
    text = (
        "<b>Halal Bot — Help</b>\n\n"
        "/start — share location and set up reminders\n"
        "Profile — stats and streaks\n"
        "Log Prayers — mark prayers, jump to any day or pick a date\n"
        "Reports — today / 7-day / 30-day summaries\n"
        "30-Day Overview — your activity grid\n"
        "View Schedule — today's upcoming alerts\n"
        "Settings — reminder lead time and Asr calculation\n\n"
        "I send a heads-up before each prayer, an alert at the exact time, "
        "and an end-of-day summary."
    )
    await message.answer(text, reply_markup=get_main_menu_keyboard())


@dp.message(Command("schedule"))
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
        time_str = job.next_run_time.strftime("%I:%M %p")
        if parts[3] == "warning":
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
        FROM prayer_logs WHERE user_id = $1 AND is_completed = TRUE;
    """
    streak_query = """
        SELECT prayer_date FROM prayer_logs
        WHERE user_id = $1 AND is_completed = TRUE
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

    processing_msg = await message.answer(
        "Calculating your timezone and prayer schedules...",
        reply_markup=ReplyKeyboardRemove()
    )
    prayer_data = await fetch_prayer_times(lat, lon)
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
    await processing_msg.delete()
    await message.answer(
        f"<b>Location registered successfully!</b>\n<b>Timezone:</b> {user_timezone}\n\n"
        f"<b>Today's Timings:</b>\nFajr: {t['Fajr']}\nDhuhr: {t['Dhuhr']}\n"
        f"Asr: {t['Asr']}\nMaghrib: {t['Maghrib']}\nIsha: {t['Isha']}\n\n"
        "I have automatically set up your prayer alerts!",
        reply_markup=get_main_menu_keyboard()
    )


@dp.message(Command("log"))
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


@dp.message(Command("report"))
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
            "WHERE user_id = $1 AND prayer_name = $2 AND prayer_date = $3",
            user_id, prayer_name, target_date
        )
    new_state = (not bool(current)) if from_menu else True

    if new_state and target_date == user_now.date():
        job = scheduler.get_job(f"reminder_{user_id}_{prayer_name}_exact")
        if job:
            await callback.answer(
                f"It is not time for {prayer_name} yet. Adhan is at {job.next_run_time.strftime('%I:%M %p')}.",
                show_alert=True
            )
            return

    upsert = """
        INSERT INTO prayer_logs (user_id, prayer_name, prayer_date, is_completed)
        VALUES ($1, $2, $3, $4)
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

@dp.message()
async def fallback_handler(message: Message) -> None:
    await message.answer(
        "I didn't understand that. Use the menu buttons or /help.",
        reply_markup=get_main_menu_keyboard()
    )


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Set location & reminders"),
        BotCommand(command="profile", description="Your stats & streaks"),
        BotCommand(command="log", description="Log prayers"),
        BotCommand(command="report", description="Daily / weekly / monthly reports"),
        BotCommand(command="overview", description="30-day activity grid"),
        BotCommand(command="schedule", description="Today's upcoming alerts"),
        BotCommand(command="settings", description="Reminder time & Asr method"),
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
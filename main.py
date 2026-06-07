import asyncio
import os
import asyncpg
import logging
import pytz
from datetime import datetime, timedelta, date
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
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


def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="Profile"), KeyboardButton(text="Log Prayers")],
        [KeyboardButton(text="View Schedule")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


async def get_user_timezone(db_pool: asyncpg.Pool, user_id: int) -> str | None:
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT timezone FROM users WHERE user_id = $1", user_id)


def user_local_date(tz_str: str | None) -> date:
    """Today's date in the user's timezone (falls back to UTC if unknown)."""
    tz = pytz.timezone(tz_str) if tz_str else pytz.UTC
    return datetime.now(tz).date()


async def send_prayer_reminder(bot: Bot, user_id: int, prayer_name: str, is_exact: bool):
    try:
        if is_exact:
            text = f"<b>It is time for {prayer_name}.</b>"
            kb = [[InlineKeyboardButton(text="Mark as Prayed", callback_data=f"pray_{prayer_name}")]]
            reply_markup = InlineKeyboardMarkup(inline_keyboard=kb)
            await bot.send_message(chat_id=user_id, text=text, reply_markup=reply_markup)
        else:
            text = f"<b>{prayer_name} is approaching!</b>\nTake a moment to prepare and make Wudu."
            await bot.send_message(chat_id=user_id, text=text)
    except Exception as e:
        logging.error(f"Failed to send reminder to {user_id}: {e}")


async def daily_scheduler_job(db_pool: asyncpg.Pool, bot: Bot, scheduler: AsyncIOScheduler):
    logging.info("Running background master job to queue today's prayer times...")

    for job in scheduler.get_jobs():
        if job.id != "daily_master_job":
            job.remove()

    async with db_pool.acquire() as conn:
        users = await conn.fetch(
            "SELECT user_id, latitude, longitude, timezone, "
            "COALESCE(reminder_offset_mins, 5) AS reminder_offset_mins "
            "FROM users WHERE latitude IS NOT NULL"
        )

    queued_count = 0
    for user in users:
        prayer_data = await fetch_prayer_times(user['latitude'], user['longitude'])
        if not prayer_data:
            continue

        user_tz = pytz.timezone(user['timezone'])
        now = datetime.now(user_tz)

        logging.info(f"--- Scheduling for User ID: {user['user_id']} ---")

        for prayer_name, time_str in prayer_data['timings'].items():
            if prayer_name not in PRAYERS:
                continue

            hour, minute = map(int, time_str.split(':'))
            adhan_dt = user_tz.localize(datetime(now.year, now.month, now.day, hour, minute))
            reminder_dt = adhan_dt - timedelta(minutes=user['reminder_offset_mins'])

            if reminder_dt > now:
                scheduler.add_job(
                    send_prayer_reminder,
                    'date',
                    run_date=reminder_dt,
                    id=f"reminder_{user['user_id']}_{prayer_name}_warning",
                    args=[bot, user['user_id'], prayer_name, False],
                )
                queued_count += 1

            if adhan_dt > now:
                scheduler.add_job(
                    send_prayer_reminder,
                    'date',
                    run_date=adhan_dt,
                    id=f"reminder_{user['user_id']}_{prayer_name}_exact",
                    args=[bot, user['user_id'], prayer_name, True],
                )
                queued_count += 1

    logging.info(f"Successfully queued {queued_count} upcoming reminders for today.")


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
        "/start — share your location and set up reminders\n"
        "Profile — see your prayer stats\n"
        "Log Prayers — manually mark prayers as completed\n"
        "View Schedule — see today's upcoming alerts\n\n"
        "I send a heads-up before each prayer and an alert at the exact time."
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
        prayer_name = parts[2]
        job_type = parts[3]

        if job_type == "warning":
            user_jobs.append(f"<b>{prayer_name} Warning:</b> {time_str}")
        elif job_type == "exact":
            user_jobs.append(f"<b>{prayer_name} Adhan:</b> {time_str}\n")

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

    query = """
        SELECT
            COUNT(*) AS total_prayers,
            COUNT(*) FILTER (WHERE prayer_date = $2) AS today_prayers
        FROM prayer_logs
        WHERE user_id = $1 AND is_completed = TRUE;
    """

    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(query, user_id, local_today)

    total = row['total_prayers'] or 0
    today = row['today_prayers'] or 0

    text = (
        f"<b>Profile: {message.from_user.full_name}</b>\n\n"
        f"<b>Prayers Logged Today:</b> {today}/5\n"
        f"<b>Lifetime Prayers Logged:</b> {total}\n\n"
        "Keep up the good work!"
    )

    await message.answer(text)


@dp.message(F.location)
async def location_handler(message: Message, db_pool: asyncpg.Pool, scheduler: AsyncIOScheduler) -> None:
    lat = message.location.latitude
    lon = message.location.longitude
    user_id = message.from_user.id
    username = message.from_user.username
    full_name = message.from_user.full_name

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
        ON CONFLICT (user_id)
        DO UPDATE SET
            username = EXCLUDED.username,
            full_name = EXCLUDED.full_name,
            timezone = EXCLUDED.timezone,
            latitude = EXCLUDED.latitude,
            longitude = EXCLUDED.longitude;
    """

    async with db_pool.acquire() as connection:
        await connection.execute(query, user_id, username, full_name, user_timezone, lat, lon)

    await daily_scheduler_job(db_pool, bot, scheduler)

    t = prayer_data['timings']
    success_text = (
        f"<b>Location registered successfully!</b>\n"
        f"<b>Timezone:</b> {user_timezone}\n\n"
        f"<b>Today's Timings:</b>\n"
        f"Fajr: {t['Fajr']}\n"
        f"Dhuhr: {t['Dhuhr']}\n"
        f"Asr: {t['Asr']}\n"
        f"Maghrib: {t['Maghrib']}\n"
        f"Isha: {t['Isha']}\n\n"
        "I have automatically set up your prayer alerts!"
    )

    await processing_msg.delete()
    await message.answer(success_text, reply_markup=get_main_menu_keyboard())


def generate_log_keyboard(target_date: date) -> InlineKeyboardMarkup:
    date_str = target_date.strftime("%Y-%m-%d")
    prev_date = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")
    next_date = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")

    kb = [
        [
            InlineKeyboardButton(text="Fajr", callback_data=f"pray_Fajr_{date_str}"),
            InlineKeyboardButton(text="Dhuhr", callback_data=f"pray_Dhuhr_{date_str}")
        ],
        [
            InlineKeyboardButton(text="Asr", callback_data=f"pray_Asr_{date_str}"),
            InlineKeyboardButton(text="Maghrib", callback_data=f"pray_Maghrib_{date_str}")
        ],
        [
            InlineKeyboardButton(text="Isha", callback_data=f"pray_Isha_{date_str}")
        ],
        [
            InlineKeyboardButton(text="< Prev", callback_data=f"nav_log_{prev_date}"),
            InlineKeyboardButton(text=date_str, callback_data="ignore"),
            InlineKeyboardButton(text="Next >", callback_data=f"nav_log_{next_date}")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


@dp.message(Command("log"))
@dp.message(F.text == "Log Prayers")
async def log_manual_handler(message: Message, db_pool: asyncpg.Pool) -> None:
    tz_str = await get_user_timezone(db_pool, message.from_user.id)
    if not tz_str:
        await message.answer("Please set your location first using /start.")
        return

    today = user_local_date(tz_str)
    await message.answer(
        f"<b>Manual Logging for {today}</b>\nSelect a prayer to mark as completed:",
        reply_markup=generate_log_keyboard(today),
    )


@dp.callback_query(F.data == "ignore")
async def ignore_callback(callback: CallbackQuery) -> None:
    await callback.answer()


@dp.callback_query(F.data.startswith("nav_log_"))
async def log_nav_handler(callback: CallbackQuery) -> None:
    date_str = callback.data.split("_")[2]
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()

    await callback.message.edit_text(
        f"<b>Manual Logging for {target_date}</b>\nSelect a prayer to mark as completed:",
        reply_markup=generate_log_keyboard(target_date),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("pray_"))
async def log_prayer_handler(callback: CallbackQuery, db_pool: asyncpg.Pool, scheduler: AsyncIOScheduler) -> None:
    parts = callback.data.split("_")
    prayer_name = parts[1]
    user_id = callback.from_user.id

    user_tz_str = await get_user_timezone(db_pool, user_id)
    if not user_tz_str:
        await callback.answer("Please set your location first using /start", show_alert=True)
        return

    user_tz = pytz.timezone(user_tz_str)
    user_now = datetime.now(user_tz)

    if len(parts) == 3:
        target_date = datetime.strptime(parts[2], "%Y-%m-%d").date()
    else:
        target_date = user_now.date()

    if target_date > user_now.date():
        await callback.answer("You cannot log prayers for future dates.", show_alert=True)
        return

    if target_date == user_now.date():
        job = scheduler.get_job(f"reminder_{user_id}_{prayer_name}_exact")
        if job:
            time_str = job.next_run_time.strftime("%I:%M %p")
            await callback.answer(f"It is not time for {prayer_name} yet. Adhan is at {time_str}.", show_alert=True)
            return

    query = """
        INSERT INTO prayer_logs (user_id, prayer_name, prayer_date, is_completed)
        VALUES ($1, $2, $3, TRUE)
        ON CONFLICT (user_id, prayer_name, prayer_date)
        DO UPDATE SET is_completed = TRUE;
    """

    async with db_pool.acquire() as connection:
        await connection.execute(query, user_id, prayer_name, target_date)

    await callback.answer(f"{prayer_name} logged for {target_date}")


@dp.message()
async def fallback_handler(message: Message) -> None:
    await message.answer(
        "I didn't understand that. Use the menu buttons or /help.",
        reply_markup=get_main_menu_keyboard()
    )


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
    scheduler.add_job(
        daily_scheduler_job,
        'cron',
        hour=0,
        minute=1,
        id="daily_master_job",
        args=[pool, bot, scheduler]
    )
    scheduler.start()

    await daily_scheduler_job(pool, bot, scheduler)

    dp.workflow_data.update({'db_pool': pool, 'scheduler': scheduler})

    try:
        logging.info("Bot is up and running...")
        await dp.start_polling(bot)
    finally:
        await pool.close()
        logging.info("Database connection closed.")


if __name__ == "__main__":
    asyncio.run(main())
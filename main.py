import asyncio
import os
import asyncpg
import logging
import pytz
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from prayer_api import fetch_prayer_times

load_dotenv()

bot = Bot(token=os.getenv('BOT_TOKEN'))
dp = Dispatcher()

async def send_prayer_reminder(bot: Bot, user_id: int, prayer_name: str, is_exact: bool):
    try:
        if is_exact:
            text = f"<b>It is time for {prayer_name}.</b>"
            kb = [[InlineKeyboardButton(text="Mark as Prayed", callback_data=f"pray_{prayer_name}")]]
            reply_markup = InlineKeyboardMarkup(inline_keyboard=kb)
            await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            text = f"<b>{prayer_name} is approaching!</b>\nTake a moment to prepare and make Wudu."
            await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Failed to send reminder to {user_id}: {e}")

async def daily_scheduler_job(db_pool: asyncpg.Pool, bot: Bot, scheduler: AsyncIOScheduler):
    print("Running background master job to queue today's prayer times...")
    
    for job in scheduler.get_jobs():
        if job.id != "daily_master_job":
            job.remove()
            
    async with db_pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id, latitude, longitude, timezone, COALESCE(reminder_offset_mins, 5) as reminder_offset_mins FROM users WHERE latitude IS NOT NULL")
        
    queued_count = 0
    for user in users:
        prayer_data = await fetch_prayer_times(user['latitude'], user['longitude'])
        if not prayer_data:
            continue
            
        user_tz = pytz.timezone(user['timezone'])
        now = datetime.now(user_tz)
        
        print(f"\n--- Scheduling for User ID: {user['user_id']} ---")
        
        for prayer_name, time_str in prayer_data['timings'].items():
            if prayer_name not in ['Fajr', 'Dhuhr', 'Asr', 'Maghrib', 'Isha']:
                continue
                
            hour, minute = map(int, time_str.split(':'))
            adhan_dt = user_tz.localize(datetime(now.year, now.month, now.day, hour, minute))
            reminder_dt = adhan_dt - timedelta(minutes=user['reminder_offset_mins'])
            
            if reminder_dt > now:
                job_id = f"reminder_{user['user_id']}_{prayer_name}_warning"
                scheduler.add_job(
                    send_prayer_reminder,
                    'date',
                    run_date=reminder_dt,
                    id=job_id,
                    args=[bot, user['user_id'], prayer_name, False]
                )
                queued_count += 1
                print(f"  -> [Warning] {prayer_name} at {reminder_dt.strftime('%H:%M:%S')}")
            
            if adhan_dt > now:
                job_id = f"reminder_{user['user_id']}_{prayer_name}_exact"
                scheduler.add_job(
                    send_prayer_reminder,
                    'date',
                    run_date=adhan_dt,
                    id=job_id,
                    args=[bot, user['user_id'], prayer_name, True]
                )
                queued_count += 1
                print(f"  -> [ Exact ] {prayer_name} at {adhan_dt.strftime('%H:%M:%S')}")

    print(f"\nSuccessfully queued {queued_count} upcoming reminders for today.")

@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    kb = [[KeyboardButton(text="Share Location", request_location=True)]]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True)
    
    await message.answer(
        f"As-salamu alaykum, {message.from_user.full_name}!\n\n"
        "To track your prayers and send accurate daily reminders, I need to know your local timezone.\n"
        "Please press the button below to share your location.",
        reply_markup=keyboard
    )

@dp.message(Command("schedule"))
async def check_schedule_handler(message: Message, scheduler: AsyncIOScheduler) -> None:
    user_id_str = str(message.from_user.id)
    user_jobs = []
    
    for job in scheduler.get_jobs():
        if user_id_str in job.id:
            time_str = job.next_run_time.strftime("%I:%M %p")
            parts = job.id.split('_')
            prayer_name = parts[2]
            job_type = parts[3]
            
            if job_type == "warning":
                user_jobs.append(f"<b>{prayer_name} Warning:</b> {time_str}")
            elif job_type == "exact":
                user_jobs.append(f"<b>{prayer_name} Adhan:</b> {time_str}\n")
                
    if not user_jobs:
        await message.answer("You have no upcoming prayer alerts scheduled for the rest of today.")
    else:
        schedule_text = "<b>Your Upcoming Alerts Today:</b>\n\n" + "\n".join(user_jobs)
        await message.answer(schedule_text, parse_mode="HTML")


# @dp.message(Command("testbutton"))
# async def test_button_handler(message: Message) -> None:
#     kb = [[InlineKeyboardButton(text="Mark as Prayed", callback_data="pray_TestPrayer")]]
#     reply_markup = InlineKeyboardMarkup(inline_keyboard=kb)
#     await message.answer("Test reminder message.", reply_markup=reply_markup)

@dp.message(Command("profile"))
async def profile_handler(message: Message, db_pool: asyncpg.Pool) -> None:
    user_id = message.from_user.id
    
    query = """
        SELECT 
            COUNT(*) AS total_prayers,
            COUNT(*) FILTER (WHERE prayer_date = CURRENT_DATE) AS today_prayers
        FROM prayer_logs 
        WHERE user_id = $1 AND is_completed = TRUE;
    """
    
    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(query, user_id)
        
    total = row['total_prayers'] if row['total_prayers'] else 0
    today = row['today_prayers'] if row['today_prayers'] else 0
    
    text = (
        f"<b>Profile: {message.from_user.full_name}</b>\n\n"
        f"<b>Prayers Logged Today:</b> {today}/5\n"
        f"<b>Lifetime Prayers Logged:</b> {total}\n\n"
        "Keep up the good work!"
    )
    
    await message.answer(text, parse_mode="HTML")

@dp.message(F.location)
async def location_handler(message: Message, db_pool: asyncpg.Pool, scheduler: AsyncIOScheduler) -> None:
    lat = message.location.latitude
    lon = message.location.longitude
    user_id = message.from_user.id
    username = message.from_user.username
    full_name = message.from_user.full_name

    processing_msg = await message.answer("Calculating your timezone and prayer schedules...", reply_markup=ReplyKeyboardRemove())

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
    await message.answer(success_text, parse_mode="HTML")

@dp.callback_query(F.data.startswith("pray_"))
async def log_prayer_handler(callback: CallbackQuery, db_pool: asyncpg.Pool) -> None:
    prayer_name = callback.data.split("_")[1]
    user_id = callback.from_user.id
    current_date = datetime.now().date()

    query = """
        INSERT INTO prayer_logs (user_id, prayer_name, prayer_date, is_completed)
        VALUES ($1, $2, $3, TRUE)
        ON CONFLICT (user_id, prayer_name, prayer_date)
        DO UPDATE SET is_completed = TRUE;
    """

    async with db_pool.acquire() as connection:
        await connection.execute(query, user_id, prayer_name, current_date)

    await callback.message.edit_text(
        f"<b>{prayer_name} has been logged as completed.</b>",
        parse_mode="HTML"
    )
    await callback.answer()

# @dp.message(Command("testalert"))
# async def test_alert_handler(message: Message) -> None:
#     text = "<b>It is time for Asr.</b>"
#     kb = [[InlineKeyboardButton(text="Mark as Prayed", callback_data="pray_Asr")]]
#     reply_markup = InlineKeyboardMarkup(inline_keyboard=kb)
#     await message.answer(text, parse_mode="HTML", reply_markup=reply_markup)

async def main() -> None:
    pool = await asyncpg.create_pool(
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        database=os.getenv('DB_NAME'),
        host=os.getenv('DB_HOST'),
        port=os.getenv('DB_PORT')
    )
    print("Database connection pool created.")
    
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
        print("Bot is up and running...")
        await dp.start_polling(bot)
    finally:
        await pool.close()
        print("Database connection closed.")

if __name__ == "__main__":
    asyncio.run(main())
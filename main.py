import asyncio
import os
import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from dotenv import load_dotenv

from prayer_api import fetch_prayer_times

load_dotenv()

bot = Bot(token=os.getenv('BOT_TOKEN'))
dp = Dispatcher()

@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    """
    Greets the user and presents a keyboard button to share their location.
    """
    kb = [
        [KeyboardButton(text="📍 Share Location", request_location=True)]
    ]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True)
    
    await message.answer(
        f"As-salamu alaykum, {message.from_user.full_name}!\n\n"
        "To track your prayers and send accurate daily reminders, I need to know your local timezone.\n"
        "Please press the button below to share your location.",
        reply_markup=keyboard
    )

@dp.message(F.location)
async def location_handler(message: Message, db_pool: asyncpg.Pool) -> None:
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

    t = prayer_data['timings']
    success_text = (
        f"<b>Location registered successfully!</b>\n"
        f"<b>Timezone:</b> {user_timezone}\n\n"
        f"<b>Today's Timings:</b>\n"
        f"• Fajr: {t['Fajr']}\n"
        f"• Dhuhr: {t['Dhuhr']}\n"
        f"• Asr: {t['Asr']}\n"
        f"• Maghrib: {t['Maghrib']}\n"
        f"• Isha: {t['Isha']}\n\n"
        "I will automatically set up your prayer alerts now."
    )
    
    await processing_msg.delete()
    await message.answer(success_text, parse_mode="HTML")

async def main() -> None:
    pool = await asyncpg.create_pool(
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        database=os.getenv('DB_NAME'),
        host=os.getenv('DB_HOST'),
        port=os.getenv('DB_PORT')
    )
    
    print("Database connection pool created.")
    dp.workflow_data.update({'db_pool': pool})

    try:
        print("Bot is up and running...")
        await dp.start_polling(bot)
    finally:
        await pool.close()
        print("Database connection closed.")

if __name__ == "__main__":
    asyncio.run(main())
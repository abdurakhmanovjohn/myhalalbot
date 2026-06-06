import asyncio
import os
import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

bot = Bot(token=os.getenv('BOT_TOKEN'))
dp = Dispatcher()

@dp.message(CommandStart())
async def command_start_handler(message: Message, db_pool: asyncpg.Pool) -> None:
    
    await message.answer(f"As-salamu alaykum, {message.from_user.full_name}! I'm your daily prayer tracker. Let's get started.")

async def main() -> None:
    pool = await asyncpg.create_pool(
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        database=os.getenv('DB_NAME'),
        host=os.getenv('DB_HOST'),
        port=os.getenv('DB_PORT')
    )
    
    print("Database connection pool created successfully.")

    dp.workflow_data.update({'db_pool': pool})

    try:
        print("Bot is up and running...")
        await dp.start_polling(bot)
    finally:
        await pool.close()
        print("Database connection closed.")

if __name__ == "__main__":
    asyncio.run(main())
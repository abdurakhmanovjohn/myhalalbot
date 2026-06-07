import asyncio
import os
import asyncpg
from dotenv import load_dotenv

load_dotenv()

async def create_tables():
    print("Connecting to database...")
    conn = await asyncpg.connect(
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        database=os.getenv('DB_NAME'),
        host=os.getenv('DB_HOST'),
        port=os.getenv('DB_PORT')
    )
    try:
        print("Creating 'users' table...")
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(255),
                full_name VARCHAR(255),
                timezone VARCHAR(50) DEFAULT 'UTC',
                latitude DOUBLE PRECISION,
                longitude DOUBLE PRECISION,
                reminder_offset_mins INTEGER DEFAULT 5
            );
        ''')

        print("Creating 'prayer_logs' table...")
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS prayer_logs (
                log_id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                prayer_name VARCHAR(20) NOT NULL,
                prayer_date DATE NOT NULL,
                is_completed BOOLEAN DEFAULT FALSE,
                UNIQUE(user_id, prayer_name, prayer_date)
            );
        ''')
        print("All tables created successfully!")
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        await conn.close()
        print("Database connection closed.")

if __name__ == "__main__":
    asyncio.run(create_tables())
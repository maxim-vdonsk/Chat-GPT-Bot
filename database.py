# database.py
import aiosqlite
import logging
from config import AVAILABLE_MODELS

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, path):
        self.path = path
        self.connection = None

    async def connect(self):
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA foreign_keys = ON")
        await self.connection.commit()

    async def init_db(self):
        async with self.connection.cursor() as cursor:
            # Create profiles table if it doesn't exist
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS profiles (
                    user_id INTEGER PRIMARY KEY,
                    name TEXT,
                    gpt_requests INTEGER DEFAULT 0,
                    image_requests INTEGER DEFAULT 0,
                    audio_requests INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Create other tables (user_settings, history, models, user_stats)
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY,
                    model_id INTEGER,
                    FOREIGN KEY (user_id) REFERENCES profiles(user_id),
                    FOREIGN KEY (model_id) REFERENCES models(id)
                )
            """)
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    message TEXT,
                    reply TEXT,
                    session_id INTEGER,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES profiles(user_id)
                )
            """)
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS models (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,
                    provider TEXT,
                    is_active INTEGER DEFAULT 1
                )
            """)
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id INTEGER,
                    date TEXT,
                    action_type TEXT,
                    model_name TEXT,
                    count INTEGER DEFAULT 1,
                    PRIMARY KEY (user_id, date, action_type, model_name),
                    FOREIGN KEY (user_id) REFERENCES profiles(user_id)
                )
            """)

            # Check and add missing columns in 'profiles'
            await cursor.execute("PRAGMA table_info(profiles)")
            columns = [row['name'] for row in await cursor.fetchall()]

            if 'audio_requests' not in columns:
                await cursor.execute("ALTER TABLE profiles ADD COLUMN audio_requests INTEGER DEFAULT 0")
                logger.info("Added audio_requests column to profiles table")

            if 'created_at' not in columns:
                await cursor.execute("ALTER TABLE profiles ADD COLUMN created_at TEXT")
                logger.info("Added created_at column to profiles table")
            
            # Всегда пытаемся обновить существующие записи, где created_at IS NULL.
            # Это важно для данных, которые могли быть созданы до добавления DEFAULT или исправления логики.
            await self.connection.execute("UPDATE profiles SET created_at = datetime('now', 'localtime') WHERE created_at IS NULL")
            async with self.connection.execute("SELECT changes()") as changes_cursor:
                updated_rows = await changes_cursor.fetchone()
                if updated_rows and updated_rows[0] > 0:
                    logger.info(f"Backfilled 'created_at' for {updated_rows[0]} profiles that had NULL.")
            
            # Коммит нужен после DML операций, таких как UPDATE
            await self.connection.commit()

            for model_name, provider in AVAILABLE_MODELS:
                try:
                    await self.connection.execute(
                        "INSERT OR IGNORE INTO models (name, provider) VALUES (?, ?)",
                        (model_name, provider)
                    )
                except Exception as e:
                    logger.error(f"Error inserting model {model_name}: {e}")

            await self.connection.commit() # Финальный коммит для init_db
            

    async def close(self):
        if self.connection:
            await self.connection.close()

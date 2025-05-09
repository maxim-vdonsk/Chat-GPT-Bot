# database.py
import aiosqlite
import logging
from config import AVAILABLE_MODELS

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.connection = None

    async def connect(self):
        self.connection = await aiosqlite.connect(self.db_path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA foreign_keys = ON")
        logger.info("Database connected")

    async def close(self):
        if self.connection:
            await self.connection.close()
            logger.info("Database connection closed")

    async def init_db(self):        
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS history (
                user_id INTEGER,
                message TEXT,
                reply TEXT,
                session_id INTEGER DEFAULT 1,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                gpt_requests INTEGER DEFAULT 0,
                image_requests INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                provider TEXT,
                is_active BOOLEAN DEFAULT 1
            )
        """)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                model_id INTEGER,
                FOREIGN KEY (model_id) REFERENCES models(id)
            )
        """)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS user_stats (
                user_id INTEGER,
                date TEXT,
                action_type TEXT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date, action_type),
                FOREIGN KEY (user_id) REFERENCES profiles(user_id)
            )
        """)

        for model_name, provider in AVAILABLE_MODELS:
            try:
                await self.connection.execute(
                    "INSERT OR IGNORE INTO models (name, provider) VALUES (?, ?)",
                    (model_name, provider)
                )
            except Exception as e:
                logger.error(f"Error inserting model {model_name}: {e}")

        await self.connection.commit()
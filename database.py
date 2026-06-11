import asyncpg
from dotenv import load_dotenv
import os

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

async def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    return await asyncpg.connect(DATABASE_URL)
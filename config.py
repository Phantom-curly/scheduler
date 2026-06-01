import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",     "")
ALLOWED_USER_ID    = int(os.getenv("ALLOWED_USER_ID", "0"))
TIMEZONE           = os.getenv("TIMEZONE",           "Asia/Seoul")
DB_PATH            = os.getenv("DB_PATH",            "planner.db")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
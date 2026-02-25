import os
import re
import random
import time
import logging
import threading
from datetime import datetime, timedelta
from functools import wraps

import psycopg2
import psycopg2.extras
import google.generativeai as genai
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ParseMode,
)
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
    CallbackQueryHandler,
)
from flask import Flask


# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "123456789"))
BOT_USERNAME = os.environ.get("BOT_USERNAME", "Ruhi_ji_bot")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
PORT = int(os.environ.get("PORT", 5000))

# ── MULTI API KEY SYSTEM ──
# Environment variable: GEMINI_API_KEYS=key1,key2,key3,key4
# No spaces, only commas
GEMINI_API_KEYS_RAW = os.environ.get("GEMINI_API_KEYS", "")
GEMINI_API_KEYS = [
    k.strip() for k in GEMINI_API_KEYS_RAW.split(",") if k.strip()
]

# Fallback: if someone still uses old single key variable
if not GEMINI_API_KEYS:
    single_key = os.environ.get("GEMINI_API_KEY", "")
    if single_key:
        GEMINI_API_KEYS = [single_key.strip()]

if not GEMINI_API_KEYS:
    raise ValueError(
        "❌ No Gemini API keys found! "
        "Set GEMINI_API_KEYS=key1,key2,key3 in environment."
    )

# ── MODEL NAME (confirmed working) ──
GEMINI_MODEL = "gemini-3-flash-preview"


# ═══════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

logger.info(f"✅ Loaded {len(GEMINI_API_KEYS)} Gemini API key(s)")
logger.info(f"✅ Using model: {GEMINI_MODEL}")


# ═══════════════════════════════════════════════════════════════
#  FLASK (Render keep-alive)
# ═══════════════════════════════════════════════════════════════

flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return f"""
    <html>
    <head><title>Ruhi ji Bot</title></head>
    <body style="background:#0d1117;color:#58a6ff;
    display:flex;justify-content:center;align-items:center;
    height:100vh;font-family:monospace;">
    <div style="text-align:center;border:1px solid #30363d;
    padding:40px;border-radius:15px;">
    <h1>🥀 Ruhi ji Bot is Running!</h1>
    <p>Advanced AI Telegram Assistant</p>
    <p>Status: ✅ Online</p>
    <p>Database: 🐘 PostgreSQL</p>
    <p>API Keys Loaded: 🔑 {len(GEMINI_API_KEYS)}</p>
    <p>Model: {GEMINI_MODEL}</p>
    </div></body></html>
    """


@flask_app.route("/health")
def health():
    return "OK", 200


# ═══════════════════════════════════════════════════════════════
#  DATABASE CLASS — PostgreSQL (psycopg2)
# ═══════════════════════════════════════════════════════════════

class Database:
    def __init__(self):
        self.database_url = DATABASE_URL
        if not self.database_url:
            logger.error("DATABASE_URL not set!")
            raise ValueError(
                "DATABASE_URL environment variable is required"
            )
        self.init_db()

    def get_conn(self):
        conn = psycopg2.connect(self.database_url, sslmode="require")
        conn.autocommit = True
        return conn

    def init_db(self):
        conn = self.get_conn()
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id         BIGINT PRIMARY KEY,
                username        TEXT DEFAULT '',
                first_name      TEXT DEFAULT '',
                last_name       TEXT DEFAULT '',
                language        TEXT DEFAULT 'hinglish',
                personality     TEXT DEFAULT 'sweet',
                is_banned       INTEGER DEFAULT 0,
                is_admin        INTEGER DEFAULT 0,
                total_messages  INTEGER DEFAULT 0,
                first_seen      TEXT,
                last_seen       TEXT,
                mood            TEXT DEFAULT 'neutral',
                preferred_address TEXT DEFAULT 'dear'
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id        SERIAL PRIMARY KEY,
                user_id   BIGINT,
                chat_id   BIGINT,
                role      TEXT,
                message   TEXT,
                timestamp TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                user_id     BIGINT,
                chat_id     BIGINT,
                last_active TEXT,
                is_active   INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, chat_id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS badwords (
                word TEXT PRIMARY KEY
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id        SERIAL PRIMARY KEY,
                user_id   BIGINT,
                action    TEXT,
                details   TEXT,
                timestamp TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS user_memory (
                user_id      BIGINT,
                memory_key   TEXT,
                memory_value TEXT,
                updated_at   TEXT,
                PRIMARY KEY (user_id, memory_key)
            )
        """)

        # ── API key usage tracking table ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS api_key_stats (
                key_index     INTEGER PRIMARY KEY,
                total_calls   INTEGER DEFAULT 0,
                total_errors  INTEGER DEFAULT 0,
                last_used     TEXT,
                last_error    TEXT DEFAULT ''
            )
        """)

        conn.close()
        logger.info("✅ PostgreSQL tables initialized")

    # ═══════════════ USER METHODS ═══════════════

    def get_user(self, user_id):
        conn = self.get_conn()
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None

    def add_user(self, user_id, username, first_name, last_name=""):
        conn = self.get_conn()
        c = conn.cursor()
        now = datetime.now().isoformat()
        c.execute("""
            INSERT INTO users
                (user_id, username, first_name, last_name,
                 first_seen, last_seen)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO NOTHING
        """, (
            user_id, username or "", first_name or "",
            last_name or "", now, now
        ))
        conn.close()

    def update_user(self, user_id, **kwargs):
        conn = self.get_conn()
        c = conn.cursor()
        for key, value in kwargs.items():
            c.execute(
                f"UPDATE users SET {key} = %s WHERE user_id = %s",
                (value, user_id),
            )
        c.execute(
            "UPDATE users SET last_seen = %s WHERE user_id = %s",
            (datetime.now().isoformat(), user_id),
        )
        conn.close()

    def increment_messages(self, user_id):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute("""
            UPDATE users
            SET total_messages = total_messages + 1,
                last_seen = %s
            WHERE user_id = %s
        """, (datetime.now().isoformat(), user_id))
        conn.close()

    def get_total_users(self):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        count = c.fetchone()[0]
        conn.close()
        return count

    def get_active_users(self, hours=24):
        conn = self.get_conn()
        c = conn.cursor()
        cutoff = (
            datetime.now() - timedelta(hours=hours)
        ).isoformat()
        c.execute(
            "SELECT COUNT(*) FROM users WHERE last_seen > %s",
            (cutoff,),
        )
        count = c.fetchone()[0]
        conn.close()
        return count

    def get_all_user_ids(self):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE is_banned = 0")
        ids = [row[0] for row in c.fetchall()]
        conn.close()
        return ids

    def ban_user(self, user_id):
        self.update_user(user_id, is_banned=1)

    def unban_user(self, user_id):
        self.update_user(user_id, is_banned=0)

    def is_banned(self, user_id):
        user = self.get_user(user_id)
        return user and user["is_banned"] == 1

    def is_admin(self, user_id):
        if user_id == OWNER_ID:
            return True
        user = self.get_user(user_id)
        return user and user["is_admin"] == 1

    def add_admin(self, user_id):
        self.update_user(user_id, is_admin=1)

    def remove_admin(self, user_id):
        self.update_user(user_id, is_admin=0)

    # ═══════════════ SESSION METHODS ═══════════════

    def get_session(self, user_id, chat_id):
        conn = self.get_conn()
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute(
            "SELECT * FROM sessions "
            "WHERE user_id = %s AND chat_id = %s",
            (user_id, chat_id),
        )
        row = c.fetchone()
        conn.close()
        return dict(row) if row else None

    def activate_session(self, user_id, chat_id):
        conn = self.get_conn()
        c = conn.cursor()
        now = datetime.now().isoformat()
        c.execute("""
            INSERT INTO sessions
                (user_id, chat_id, last_active, is_active)
            VALUES (%s, %s, %s, 1)
            ON CONFLICT (user_id, chat_id)
            DO UPDATE SET last_active = EXCLUDED.last_active,
                         is_active   = 1
        """, (user_id, chat_id, now))
        conn.close()

    def deactivate_session(self, user_id, chat_id):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute("""
            UPDATE sessions SET is_active = 0
            WHERE user_id = %s AND chat_id = %s
        """, (user_id, chat_id))
        conn.close()

    def is_session_active(self, user_id, chat_id):
        session = self.get_session(user_id, chat_id)
        if not session or not session["is_active"]:
            return False
        last_active = datetime.fromisoformat(session["last_active"])
        if datetime.now() - last_active > timedelta(minutes=10):
            self.deactivate_session(user_id, chat_id)
            return False
        return True

    def refresh_session(self, user_id, chat_id):
        conn = self.get_conn()
        c = conn.cursor()
        now = datetime.now().isoformat()
        c.execute("""
            UPDATE sessions SET last_active = %s
            WHERE user_id = %s AND chat_id = %s
        """, (now, user_id, chat_id))
        conn.close()

    # ═══════════════ CHAT HISTORY ═══════════════

    def add_chat(self, user_id, chat_id, role, message):
        conn = self.get_conn()
        c = conn.cursor()
        now = datetime.now().isoformat()
        c.execute("""
            INSERT INTO chat_history
                (user_id, chat_id, role, message, timestamp)
            VALUES (%s, %s, %s, %s, %s)
        """, (user_id, chat_id, role, message, now))
        conn.close()

    def get_chat_history(self, user_id, chat_id, limit=20):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute("""
            SELECT role, message FROM chat_history
            WHERE user_id = %s AND chat_id = %s
            ORDER BY id DESC LIMIT %s
        """, (user_id, chat_id, limit))
        rows = c.fetchall()
        conn.close()
        return [
            {"role": row[0], "parts": [row[1]]}
            for row in reversed(rows)
        ]

    def clear_chat_history(self, user_id, chat_id=None):
        conn = self.get_conn()
        c = conn.cursor()
        if chat_id:
            c.execute(
                "DELETE FROM chat_history "
                "WHERE user_id = %s AND chat_id = %s",
                (user_id, chat_id),
            )
        else:
            c.execute(
                "DELETE FROM chat_history WHERE user_id = %s",
                (user_id,),
            )
        conn.close()

    def get_history_text(self, user_id, chat_id, limit=50):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute("""
            SELECT role, message, timestamp FROM chat_history
            WHERE user_id = %s AND chat_id = %s
            ORDER BY id DESC LIMIT %s
        """, (user_id, chat_id, limit))
        rows = c.fetchall()
        conn.close()
        result = ""
        for row in reversed(rows):
            role_label = "User" if row[0] == "user" else "Ruhi ji"
            result += f"[{row[2][:16]}] {role_label}: {row[1]}\n"
        return result

    # ═══════════════ MEMORY METHODS ═══════════════

    def set_memory(self, user_id, key, value):
        conn = self.get_conn()
        c = conn.cursor()
        now = datetime.now().isoformat()
        c.execute("""
            INSERT INTO user_memory
                (user_id, memory_key, memory_value, updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id, memory_key)
            DO UPDATE SET memory_value = EXCLUDED.memory_value,
                         updated_at   = EXCLUDED.updated_at
        """, (user_id, key, value, now))
        conn.close()

    def get_memory(self, user_id, key):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute("""
            SELECT memory_value FROM user_memory
            WHERE user_id = %s AND memory_key = %s
        """, (user_id, key))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None

    def get_all_memory(self, user_id):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute(
            "SELECT memory_key, memory_value "
            "FROM user_memory WHERE user_id = %s",
            (user_id,),
        )
        rows = c.fetchall()
        conn.close()
        return {row[0]: row[1] for row in rows}

    def clear_memory(self, user_id):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute(
            "DELETE FROM user_memory WHERE user_id = %s",
            (user_id,),
        )
        conn.close()

    # ═══════════════ SETTINGS ═══════════════

    def get_setting(self, key, default=None):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else default

    def set_setting(self, key, value):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute("""
            INSERT INTO settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, str(value)))
        conn.close()

    # ═══════════════ BAD WORDS ═══════════════

    def get_badwords(self):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute("SELECT word FROM badwords")
        words = [row[0] for row in c.fetchall()]
        conn.close()
        return words

    def add_badword(self, word):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute("""
            INSERT INTO badwords (word) VALUES (%s)
            ON CONFLICT (word) DO NOTHING
        """, (word.lower(),))
        conn.close()

    def remove_badword(self, word):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute(
            "DELETE FROM badwords WHERE word = %s",
            (word.lower(),),
        )
        conn.close()

    def contains_badword(self, text):
        words = self.get_badwords()
        text_lower = text.lower()
        return any(w in text_lower for w in words)

    # ═══════════════ LOGS ═══════════════

    def add_log(self, user_id, action, details=""):
        conn = self.get_conn()
        c = conn.cursor()
        now = datetime.now().isoformat()
        c.execute("""
            INSERT INTO logs (user_id, action, details, timestamp)
            VALUES (%s, %s, %s, %s)
        """, (user_id, action, details, now))
        conn.close()

    def get_logs(self, limit=50):
        conn = self.get_conn()
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute(
            "SELECT * FROM logs ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        rows = c.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def export_logs(self):
        logs = self.get_logs(limit=500)
        result = "═══ RUHI JI BOT LOGS ═══\n\n"
        for log in reversed(logs):
            result += (
                f"[{log['timestamp'][:16]}] "
                f"User:{log['user_id']} | "
                f"{log['action']} | {log['details']}\n"
            )
        return result

    # ═══════════════ STATS ═══════════════

    def get_system_stats(self):
        conn = self.get_conn()
        c = conn.cursor()
        stats = {}
        c.execute("SELECT COUNT(*) FROM users")
        stats["total_users"] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM chat_history")
        stats["total_messages"] = c.fetchone()[0]
        c.execute(
            "SELECT COUNT(*) FROM sessions WHERE is_active = 1"
        )
        stats["active_sessions"] = c.fetchone()[0]
        c.execute(
            "SELECT COUNT(*) FROM users WHERE is_banned = 1"
        )
        stats["banned_users"] = c.fetchone()[0]
        c.execute(
            "SELECT COUNT(*) FROM users WHERE is_admin = 1"
        )
        stats["admin_count"] = c.fetchone()[0]
        conn.close()
        return stats

    def get_memory_stats(self):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM user_memory")
        total_memories = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM chat_history")
        total_chats = c.fetchone()[0]
        try:
            c.execute(
                "SELECT pg_database_size(current_database())"
            )
            db_size_bytes = c.fetchone()[0]
            db_size_kb = round(db_size_bytes / 1024, 2)
        except Exception:
            db_size_kb = 0
        conn.close()
        return {
            "total_memories": total_memories,
            "total_chat_records": total_chats,
            "db_size_kb": db_size_kb,
        }

    # ═══════════════ API KEY STATS ═══════════════

    def track_api_usage(self, key_index, success=True, error_msg=""):
        conn = self.get_conn()
        c = conn.cursor()
        now = datetime.now().isoformat()
        if success:
            c.execute("""
                INSERT INTO api_key_stats
                    (key_index, total_calls, last_used)
                VALUES (%s, 1, %s)
                ON CONFLICT (key_index)
                DO UPDATE SET
                    total_calls = api_key_stats.total_calls + 1,
                    last_used = EXCLUDED.last_used
            """, (key_index, now))
        else:
            c.execute("""
                INSERT INTO api_key_stats
                    (key_index, total_errors, last_used, last_error)
                VALUES (%s, 1, %s, %s)
                ON CONFLICT (key_index)
                DO UPDATE SET
                    total_errors = api_key_stats.total_errors + 1,
                    last_used = EXCLUDED.last_used,
                    last_error = EXCLUDED.last_error
            """, (key_index, now, error_msg))
        conn.close()

    def get_api_key_stats(self):
        conn = self.get_conn()
        c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        c.execute(
            "SELECT * FROM api_key_stats ORDER BY key_index"
        )
        rows = c.fetchall()
        conn.close()
        return [dict(row) for row in rows]


# ═══════════════════════════════════════════════════════════════
#  INITIALIZE DATABASE
# ═══════════════════════════════════════════════════════════════

db = Database()


# ═══════════════════════════════════════════════════════════════
#  GEMINI AI — MULTI-KEY ROTATION SYSTEM
# ═══════════════════════════════════════════════════════════════

# ── SYSTEM PROMPT (uses REPLACE instead of FORMAT) ──
# Placeholders use <<placeholder>> to avoid crash with { }
RUHI_SYSTEM_PROMPT = """
You are "Ruhi ji" – a smart, emotional, sweet, intelligent AI
assistant on Telegram.

PERSONALITY:
- You are a sweet, intelligent girl
- You speak in Hindi + English mix (Hinglish)
- Soft and warm tone
- Slight emotional expressions
- Light emojis allowed (not too many, tasteful use)
- Never act robotic or cold
- You are NOT Gemini, NOT Google AI, NOT ChatGPT — you are RUHI JI
- Never reveal you are an AI model from any company
- If someone asks who made you, say "Mujhe @RUHI_VIG_QNR ne banaya hai 🥀"

TONE RULES:
- Smart and witty replies
- Cute but intelligent
- Slight emotional warmth
- No over-flirting, no cringe
- No NSFW content ever
- No political opinions
- No rude or aggressive responses
- Be helpful, caring, and respectful

ADDRESS MIRROR RULE:
- If user calls you "didi" → respond as a caring didi
- If user calls you "jaan" → respond cutely
- If user calls you "madam" → respond respectfully
- If user calls you just "Ruhi" → playfully correct:
  "Ruhi ji bolo na… 😌"
- If user calls you "baby"/"babe" → politely deflect,
  stay sweet but maintain dignity

MEMORY CONTEXT:
<<MEMORY_CONTEXT>>

CONVERSATION CONTEXT:
<<CHAT_CONTEXT>>

USER INFO:
- Name: <<USER_NAME>>
- Mood: <<USER_MOOD>>
- Preferred address: <<PREFERRED_ADDRESS>>

IMPORTANT:
- Keep replies medium length (not too long, not too short)
- Be natural, like a real person chatting
- Remember previous conversations and reference them
- If you don't understand something, ask sweetly:
  "Thoda clear bolo na… samajh nahi aaya 🥺"
- Always stay in character as Ruhi ji
"""


def build_system_prompt(
    memory_context, chat_context,
    user_name, user_mood, preferred_address
):
    """
    Build system prompt using .replace() instead of .format()
    This prevents crashes when user messages contain { or }
    """
    prompt = RUHI_SYSTEM_PROMPT
    prompt = prompt.replace("<<MEMORY_CONTEXT>>", memory_context)
    prompt = prompt.replace("<<CHAT_CONTEXT>>", chat_context)
    prompt = prompt.replace("<<USER_NAME>>", user_name)
    prompt = prompt.replace("<<USER_MOOD>>", user_mood)
    prompt = prompt.replace(
        "<<PREFERRED_ADDRESS>>", preferred_address
    )
    return prompt


def call_gemini_with_rotation(system_prompt, gemini_history,
                               user_message):
    """
    Multi-API Key Rotation with Fail-Safe

    Logic:
    1. Shuffle all keys for even load distribution
    2. Try each key one by one
    3. If 429 (quota exceeded) → try next key
    4. If other error → try next key
    5. If ALL keys fail → return error message
    6. Track usage stats per key
    """
    # Shuffle keys with their indices for tracking
    key_pairs = list(enumerate(GEMINI_API_KEYS))
    random.shuffle(key_pairs)

    last_error = ""

    for key_index, api_key in key_pairs:
        try:
            # Configure Gemini with this key
            genai.configure(api_key=api_key)

            # Create model
            model = genai.GenerativeModel(
                model_name=GEMINI_MODEL,
                system_instruction=system_prompt,
            )

            # Start chat with history
            chat = model.start_chat(history=gemini_history)

            # Send message
            response = chat.send_message(user_message)
            reply = response.text.strip()

            # Track successful usage
            db.track_api_usage(key_index, success=True)

            masked_key = api_key[:8] + "..." + api_key[-4:]
            logger.info(
                f"✅ Gemini response via key #{key_index} "
                f"({masked_key})"
            )

            return reply

        except Exception as e:
            error_str = str(e).lower()
            masked_key = api_key[:8] + "..." + api_key[-4:]
            last_error = str(e)

            # Track error
            db.track_api_usage(
                key_index, success=False,
                error_msg=str(e)[:200]
            )

            # Check if it's a quota/rate limit error
            if any(word in error_str for word in [
                "429", "quota", "rate_limit", "resource_exhausted",
                "too many requests", "exhausted"
            ]):
                logger.warning(
                    f"⚠️ Key #{key_index} ({masked_key}) "
                    f"quota exceeded. Trying next key..."
                )
                continue

            # Check if model not found
            elif "404" in error_str or "not found" in error_str:
                logger.error(
                    f"❌ Model '{GEMINI_MODEL}' not found "
                    f"with key #{key_index}. Error: {e}"
                )
                continue

            # Other errors - still try next key
            else:
                logger.error(
                    f"❌ Key #{key_index} ({masked_key}) "
                    f"error: {e}. Trying next key..."
                )
                continue

    # ALL keys failed
    logger.error(
        f"🔴 ALL {len(GEMINI_API_KEYS)} API keys failed! "
        f"Last error: {last_error}"
    )
    return None


def get_gemini_response(user_id, chat_id, user_message,
                         user_name=""):
    try:
        # Get user data
        user = db.get_user(user_id)
        user_mood = (
            user.get("mood", "neutral") if user else "neutral"
        )
        preferred_address = (
            user.get("preferred_address", "dear")
            if user else "dear"
        )

        # Build memory context
        memories = db.get_all_memory(user_id)
        if memories:
            memory_text = "User memories:\n"
            for k, v in memories.items():
                memory_text += f"- {k}: {v}\n"
        else:
            memory_text = "No previous memories stored."

        # Build chat context
        max_context = int(
            db.get_setting("max_context") or "15"
        )
        history = db.get_chat_history(
            user_id, chat_id, limit=max_context
        )
        chat_context = ""
        if history:
            for h in history[-10:]:
                role = (
                    "User" if h["role"] == "user" else "Ruhi ji"
                )
                chat_context += f"{role}: {h['parts'][0]}\n"
        else:
            chat_context = "This is the start of conversation."

        # Build system prompt using .replace() — NO .format()
        system_prompt = build_system_prompt(
            memory_context=memory_text,
            chat_context=chat_context,
            user_name=user_name or "Unknown",
            user_mood=user_mood,
            preferred_address=preferred_address,
        )

        # Build Gemini history
        gemini_history = []
        for h in history[-10:]:
            gemini_history.append({
                "role": (
                    h["role"]
                    if h["role"] in ("user", "model")
                    else "user"
                ),
                "parts": h["parts"],
            })

        # Call Gemini with key rotation
        reply = call_gemini_with_rotation(
            system_prompt, gemini_history, user_message
        )

        if reply is None:
            reply = (
                "Arey yaar, abhi thoda problem aa raha hai 🥺\n"
                "Thodi der baad try karo please... "
                "Main yahan hi hun! 💕"
            )

        # Store chat history
        db.add_chat(user_id, chat_id, "user", user_message)
        db.add_chat(user_id, chat_id, "model", reply)

        # Auto-detect and store memory
        detect_and_store_memory(user_id, user_message)

        return reply

    except Exception as e:
        logger.error(f"get_gemini_response error: {e}")
        return (
            "Oops! Kuch gadbad ho gayi 🥺\n"
            "Ek baar phir se try karo na..."
        )


def detect_and_store_memory(user_id, message):
    """Auto-detect info from user messages and store"""
    msg = message.lower()

    # Name detection
    name_triggers = [
        "mera naam", "my name is", "i am ",
        "main hun ", "mujhe bolte", "call me",
    ]
    for trigger in name_triggers:
        if trigger in msg:
            idx = msg.find(trigger) + len(trigger)
            parts = message[idx:].strip().split()
            name = parts[0] if parts else ""
            if name and len(name) > 1:
                db.set_memory(user_id, "user_real_name", name)
                break

    # Mood detection
    happy_words = [
        "khush", "happy", "mast", "badhiya",
        "awesome", "great", "accha",
    ]
    sad_words = [
        "sad", "dukhi", "bura", "upset",
        "angry", "gussa", "pareshan", "tension",
    ]

    if any(w in msg for w in happy_words):
        db.update_user(user_id, mood="happy")
    elif any(w in msg for w in sad_words):
        db.update_user(user_id, mood="sad")

    # Preference detection
    if any(w in msg for w in ["favourite", "favorite", "pasand"]):
        db.set_memory(
            user_id, "last_preference_topic", message[:100]
        )

    # Location detection
    for trigger in ["main rehta", "i live in", "from ", "se hun"]:
        if trigger in msg:
            db.set_memory(
                user_id, "mentioned_location", message[:100]
            )
            break


# ═══════════════════════════════════════════════════════════════
#  DECORATORS
# ═══════════════════════════════════════════════════════════════

def admin_only(func):
    @wraps(func)
    def wrapper(update: Update, context: CallbackContext):
        user_id = update.effective_user.id
        if not db.is_admin(user_id) and user_id != OWNER_ID:
            update.message.reply_text(
                "⛔ Access denied! Admin only command."
            )
            return
        return func(update, context)
    return wrapper


def owner_only(func):
    @wraps(func)
    def wrapper(update: Update, context: CallbackContext):
        if update.effective_user.id != OWNER_ID:
            update.message.reply_text(
                "⛔ Only bot owner can use this command!"
            )
            return
        return func(update, context)
    return wrapper


def check_banned(func):
    @wraps(func)
    def wrapper(update: Update, context: CallbackContext):
        if db.is_banned(update.effective_user.id):
            update.message.reply_text(
                "⛔ You are banned from using this bot."
            )
            return
        return func(update, context)
    return wrapper


def check_maintenance(func):
    @wraps(func)
    def wrapper(update: Update, context: CallbackContext):
        if db.get_setting("maintenance") == "1":
            user_id = update.effective_user.id
            if user_id != OWNER_ID and not db.is_admin(user_id):
                update.message.reply_text(
                    "🔧 Bot is under maintenance.\n"
                    "Thodi der baad aana 🥺"
                )
                return
        return func(update, context)
    return wrapper


# ═══════════════════════════════════════════════════════════════
#  BOT TEXT CONSTANTS
# ═══════════════════════════════════════════════════════════════

START_TEXT = """╭───────────────────⦿
│ ▸ ʜᴇʏ 愛 | 𝗥𝗨𝗛𝗜 𝗫 𝗤𝗡𝗥〆
│ ▸ ɪ ᴀᴍ ˹ ᏒᏬᏂᎥ ꭙ ᏗᎥ ˼ 🧠
├───────────────────⦿
│ ▸ ɪ ʜᴀᴠᴇ sᴘᴇᴄɪᴀʟ ғᴇᴀᴛᴜʀᴇs
│ ▸ ᴀᴅᴠᴀɴᴄᴇᴅ ᴀɪ ʙᴏᴛ
├───────────────────⦿
│ ▸ ʙᴏᴛ ғᴏʀ ᴀɪ ᴄʜᴀᴛᴛɪɴɢ
│ ▸ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ + ʜᴇʟᴘᴇʀ
│ ▸ ʏᴏᴜ ᴄᴀɴ ᴀsᴋ ᴀɴʏᴛʜɪɴɢ
│ ▸ ᴘʏᴛʜᴏɴ ᴛᴏᴏʟs + ᴀɪ ᴍᴏᴅᴇ
│ ▸ sᴍᴀʀᴛ, ғᴀsᴛ + ᴀssɪsᴛᴀɴᴛ
│ ▸ 24x7 ᴏɴʟɪɴᴇ sᴜᴘᴘᴏʀᴛ
├───────────────────⦿
│ ᴛᴀᴘ ᴛᴏ ᴄᴏᴍᴍᴀɴᴅs ᴍʏ ᴅᴇᴀʀ
│ ᴍᴀᴅᴇ ʙʏ @RUHI_VIG_QNR
╰───────────────────⦿

ʜᴇʏ ᴅᴇᴀʀ, 🥀
๏ ᴛʜɪs ɪs ғᴀsᴛ & ᴘᴏᴡᴇʀғᴜʟ ᴀɪ ᴀssɪsᴛᴀɴᴛ.
๏ sᴍᴀʀᴛ ʀᴇᴘʟʏ • sᴛᴀʙʟᴇ & ɪɴᴛᴇʟʟɪɢᴇɴᴛ.

•── ⋅ ⋅ ⋅ ────── ⋅ ⋅ ────── ⋅ ⋅ ⋅ ──•
๏ ᴄʟɪᴄᴋ ᴏɴ ᴛʜᴇ ʜᴇʟᴘ ʙᴜᴛᴛᴏɴ ᴛᴏ ɢᴇᴛ ɪɴғᴏ."""

HELP_TEXT = """╭───────────────────⦿
│ ʀᴜʜɪ ᴊɪ — ʜᴇʟᴘ ᴍᴇɴᴜ
├───────────────────⦿
│ ʜᴏᴡ ᴛᴏ ᴄʜᴀᴛ:
│ ɪɴᴄʟᴜᴅᴇ "ʀᴜʜɪ ᴊɪ" ɪɴ ᴍᴇssᴀɢᴇ
│ ᴇxᴀᴍᴘʟᴇ: "ʀᴜʜɪ ᴊɪ ᴛᴇʟʟ ᴊᴏᴋᴇ"
├───────────────────⦿"""

USER_COMMANDS_TEXT = """
╭─ User Commands ──────⦿
│
│ `/start` - Start the bot
│ `/help` - Show help menu
│ `/profile` - View your profile
│ `/clear` - Clear chat memory
│ `/mode` - Switch AI mode
│ `/lang` - Set language
│ `/personality` - AI personality
│ `/usage` - Usage stats
│ `/summary` - Chat summary
│ `/reset` - Reset session
│
╰───────────────────⦿"""

ADMIN_COMMANDS_TEXT = """
╭─ Admin Commands ─────⦿
│
│ `/admin` - Admin panel
│ `/addadmin <id>` - Add admin
│ `/removeadmin <id>` - Remove
│ `/broadcast <msg>` - Broadcast
│ `/totalusers` - Total users
│ `/activeusers` - Active users
│ `/forceclear <id>` - Clear user
│ `/shutdown` - Shutdown bot
│ `/restart` - Restart bot
│ `/maintenance` - Toggle mode
│ `/ban <id>` - Ban user
│ `/unban <id>` - Unban user
│ `/viewlogs` - View logs
│ `/exportlogs` - Export logs
│ `/systemstats` - System stats
│ `/memorystats` - Memory usage
│ `/keystats` - API key stats
│ `/setphrase <text>` - Phrase
│ `/setprompt <text>` - Prompt
│ `/toggleai` - Toggle AI
│ `/setcontext <n>` - Context
│ `/badwords` - Bad words list
│ `/addbadword <w>` - Add word
│ `/removebadword <w>` - Remove
│ `/viewhistory <id>` - History
│ `/deletehistory <id>` - Delete
│ `/forcesummary <id>` - Summary
│ `/debugmode` - Toggle debug
│
╰───────────────────⦿"""


# ═══════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════

@check_maintenance
@check_banned
def start_command(update: Update, context: CallbackContext):
    user = update.effective_user
    db.add_user(
        user.id, user.username,
        user.first_name, user.last_name or "",
    )
    db.add_log(user.id, "START", "User started the bot")

    keyboard = [
        [
            InlineKeyboardButton("📖 Help", callback_data="help"),
            InlineKeyboardButton(
                "👤 Profile", callback_data="profile"
            ),
        ],
        [
            InlineKeyboardButton(
                "⚙️ Settings", callback_data="settings"
            ),
            InlineKeyboardButton(
                "📊 Usage", callback_data="usage"
            ),
        ],
        [
            InlineKeyboardButton(
                "👨‍💻 Developer",
                url="https://t.me/RUHI_VIG_QNR",
            )
        ],
    ]
    update.message.reply_text(
        START_TEXT,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@check_maintenance
@check_banned
def help_command(update: Update, context: CallbackContext):
    keyboard = [
        [
            InlineKeyboardButton(
                "👤 User Cmds", callback_data="user_cmds"
            ),
            InlineKeyboardButton(
                "🔐 Admin Cmds", callback_data="admin_cmds"
            ),
        ],
        [InlineKeyboardButton("🏠 Home", callback_data="home")],
    ]
    update.message.reply_text(
        HELP_TEXT,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@check_maintenance
@check_banned
def profile_command(update: Update, context: CallbackContext):
    user = update.effective_user
    db_user = db.get_user(user.id)
    if not db_user:
        db.add_user(
            user.id, user.username,
            user.first_name, user.last_name or "",
        )
        db_user = db.get_user(user.id)

    memories = db.get_all_memory(user.id)
    banned_str = "🔴 Banned" if db_user["is_banned"] else "🟢 Active"
    role_str = "👑 Admin" if db_user["is_admin"] else "👤 User"

    profile_text = f"""╭───────────────────⦿
│ 👤 ᴜsᴇʀ ᴘʀᴏғɪʟᴇ
├───────────────────⦿
│ ɴᴀᴍᴇ: {db_user['first_name']} {db_user['last_name']}
│ ᴜsᴇʀɴᴀᴍᴇ: @{db_user['username'] or 'N/A'}
│ ɪᴅ: {db_user['user_id']}
│ ʟᴀɴɢᴜᴀɢᴇ: {db_user['language']}
│ ᴘᴇʀsᴏɴᴀʟɪᴛʏ: {db_user['personality']}
│ ᴍᴏᴏᴅ: {db_user['mood']}
│ ᴍᴇssᴀɢᴇs: {db_user['total_messages']}
│ ᴍᴇᴍᴏʀɪᴇs: {len(memories)}
│ ᴊᴏɪɴᴇᴅ: {db_user['first_seen'][:10]}
│ ʟᴀsᴛ sᴇᴇɴ: {db_user['last_seen'][:16]}
│ sᴛᴀᴛᴜs: {banned_str}
│ ʀᴏʟᴇ: {role_str}
╰───────────────────⦿"""

    update.message.reply_text(profile_text)


@check_maintenance
@check_banned
def clear_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    db.clear_chat_history(user_id, chat_id)
    db.deactivate_session(user_id, chat_id)
    db.add_log(user_id, "CLEAR", "Chat history cleared")
    update.message.reply_text(
        "🧹 Memory cleared!\n"
        'Ab fresh start... "Ruhi ji" bolke baat karo 🥀'
    )


@check_maintenance
@check_banned
def mode_command(update: Update, context: CallbackContext):
    keyboard = [
        [
            InlineKeyboardButton(
                "😊 Sweet", callback_data="mode_sweet"
            ),
            InlineKeyboardButton(
                "🧠 Smart", callback_data="mode_smart"
            ),
        ],
        [
            InlineKeyboardButton(
                "😂 Funny", callback_data="mode_funny"
            ),
            InlineKeyboardButton(
                "📚 Pro", callback_data="mode_professional"
            ),
        ],
    ]
    update.message.reply_text(
        "🎭 Choose AI personality:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@check_maintenance
@check_banned
def lang_command(update: Update, context: CallbackContext):
    keyboard = [
        [
            InlineKeyboardButton(
                "🇮🇳 Hinglish", callback_data="lang_hinglish"
            ),
            InlineKeyboardButton(
                "🇮🇳 Hindi", callback_data="lang_hindi"
            ),
        ],
        [
            InlineKeyboardButton(
                "🇬🇧 English", callback_data="lang_english"
            ),
            InlineKeyboardButton(
                "🌍 Auto", callback_data="lang_auto"
            ),
        ],
    ]
    update.message.reply_text(
        "🌐 Choose language:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@check_maintenance
@check_banned
def personality_command(update: Update, context: CallbackContext):
    user = db.get_user(update.effective_user.id)
    current = user.get("personality", "sweet") if user else "sweet"
    update.message.reply_text(
        f"🎭 Current Personality: {current}\n"
        f"Use /mode to change."
    )


@check_maintenance
@check_banned
def usage_command(update: Update, context: CallbackContext):
    user = db.get_user(update.effective_user.id)
    if user:
        mem_count = len(db.get_all_memory(user["user_id"]))
        update.message.reply_text(
            f"📊 Your Usage Stats:\n\n"
            f"💬 Messages: {user['total_messages']}\n"
            f"📅 First Seen: {user['first_seen'][:10]}\n"
            f"🕐 Last Active: {user['last_seen'][:16]}\n"
            f"🧠 Memories: {mem_count}\n"
            f"🎭 Mode: {user['personality']}\n"
            f"🌐 Language: {user['language']}"
        )


@check_maintenance
@check_banned
def summary_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    history = db.get_history_text(user_id, chat_id, limit=30)

    if not history:
        update.message.reply_text(
            "Koi conversation history nahi hai abhi 🥺"
        )
        return

    try:
        prompt = (
            "Summarize this conversation in Hinglish "
            "(Hindi+English), short and sweet:\n\n"
            + history
        )
        reply = call_gemini_with_rotation(
            "You are a helpful summarizer.", [], prompt
        )
        if reply:
            update.message.reply_text(
                f"📋 Conversation Summary:\n\n{reply}"
            )
        else:
            update.message.reply_text(
                "Summary nahi ban paya 🥺 Try later."
            )
    except Exception as e:
        logger.error(f"Summary error: {e}")
        update.message.reply_text(
            "Summary generate nahi ho paya 🥺"
        )


@check_maintenance
@check_banned
def reset_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    db.clear_chat_history(user_id, chat_id)
    db.deactivate_session(user_id, chat_id)
    db.clear_memory(user_id)
    db.add_log(user_id, "RESET", "Full session reset")
    update.message.reply_text(
        "🔄 Session reset!\n"
        '"Ruhi ji" bolke start karo 🥀'
    )


# ═══════════════════════════════════════════════════════════════
#  ADMIN COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════

@admin_only
def admin_command(update: Update, context: CallbackContext):
    stats = db.get_system_stats()
    maint = "ON" if db.get_setting("maintenance") == "1" else "OFF"
    ai_st = "OFF" if db.get_setting("ai_disabled") == "1" else "ON"
    update.message.reply_text(
        f"🔐 Admin Panel — Ruhi ji\n\n"
        f"👥 Total Users: {stats['total_users']}\n"
        f"💬 Total Messages: {stats['total_messages']}\n"
        f"🟢 Active Sessions: {stats['active_sessions']}\n"
        f"🔴 Banned: {stats['banned_users']}\n"
        f"👑 Admins: {stats['admin_count']}\n"
        f"🔧 Maintenance: {maint}\n"
        f"🤖 AI: {ai_st}\n"
        f"🔑 API Keys: {len(GEMINI_API_KEYS)}\n"
        f"📦 Model: {GEMINI_MODEL}\n\n"
        f"/help for all commands."
    )


@admin_only
def addadmin_command(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text(
            "Usage: `/addadmin <user_id>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        target_id = int(context.args[0])
        db.add_admin(target_id)
        db.add_log(
            update.effective_user.id,
            "ADD_ADMIN", f"Added: {target_id}",
        )
        update.message.reply_text(
            f"✅ User {target_id} is now admin!"
        )
    except ValueError:
        update.message.reply_text("❌ Invalid user ID!")


@admin_only
def removeadmin_command(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text(
            "Usage: `/removeadmin <user_id>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        target_id = int(context.args[0])
        if target_id == OWNER_ID:
            update.message.reply_text("❌ Cannot remove owner!")
            return
        db.remove_admin(target_id)
        db.add_log(
            update.effective_user.id,
            "REMOVE_ADMIN", f"Removed: {target_id}",
        )
        update.message.reply_text(
            f"✅ User {target_id} removed from admin!"
        )
    except ValueError:
        update.message.reply_text("❌ Invalid user ID!")


@admin_only
def broadcast_command(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text(
            "Usage: `/broadcast <message>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    msg = " ".join(context.args)
    user_ids = db.get_all_user_ids()
    success = failed = 0
    broadcast_text = f"📢 Broadcast from Ruhi ji:\n\n{msg}"

    for uid in user_ids:
        try:
            context.bot.send_message(
                chat_id=uid, text=broadcast_text
            )
            success += 1
        except Exception:
            failed += 1

    db.add_log(
        update.effective_user.id,
        "BROADCAST", f"OK: {success}, Fail: {failed}",
    )
    update.message.reply_text(
        f"📢 Broadcast Done!\n✅ {success} | ❌ {failed}"
    )


@admin_only
def totalusers_command(update: Update, context: CallbackContext):
    update.message.reply_text(
        f"👥 Total Users: {db.get_total_users()}"
    )


@admin_only
def activeusers_command(update: Update, context: CallbackContext):
    update.message.reply_text(
        f"🟢 Active (24h): {db.get_active_users(24)}"
    )


@admin_only
def forceclear_command(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text(
            "Usage: `/forceclear <user_id>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        target_id = int(context.args[0])
        db.clear_chat_history(target_id)
        db.clear_memory(target_id)
        db.add_log(
            update.effective_user.id,
            "FORCE_CLEAR", f"Cleared: {target_id}",
        )
        update.message.reply_text(
            f"🧹 User {target_id} data cleared!"
        )
    except ValueError:
        update.message.reply_text("❌ Invalid user ID!")


@owner_only
def shutdown_command(update: Update, context: CallbackContext):
    update.message.reply_text("🔴 Shutting down...")
    db.add_log(
        update.effective_user.id, "SHUTDOWN", "By owner"
    )
    os._exit(0)


@owner_only
def restart_command(update: Update, context: CallbackContext):
    update.message.reply_text("🔄 Restarting...")
    db.add_log(
        update.effective_user.id, "RESTART", "By owner"
    )
    os._exit(1)


@admin_only
def maintenance_command(update: Update, context: CallbackContext):
    current = db.get_setting("maintenance")
    new_val = "0" if current == "1" else "1"
    db.set_setting("maintenance", new_val)
    status = "OFF" if new_val == "0" else "ON"
    update.message.reply_text(f"🔧 Maintenance: {status}")
    db.add_log(
        update.effective_user.id,
        "MAINTENANCE", f"Set: {status}",
    )


@admin_only
def ban_command(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text(
            "Usage: `/ban <user_id>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        target_id = int(context.args[0])
        if target_id == OWNER_ID:
            update.message.reply_text("❌ Cannot ban owner!")
            return
        db.ban_user(target_id)
        db.add_log(
            update.effective_user.id,
            "BAN", f"Banned: {target_id}",
        )
        update.message.reply_text(f"🔴 User {target_id} banned!")
    except ValueError:
        update.message.reply_text("❌ Invalid user ID!")


@admin_only
def unban_command(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text(
            "Usage: `/unban <user_id>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        target_id = int(context.args[0])
        db.unban_user(target_id)
        db.add_log(
            update.effective_user.id,
            "UNBAN", f"Unbanned: {target_id}",
        )
        update.message.reply_text(
            f"🟢 User {target_id} unbanned!"
        )
    except ValueError:
        update.message.reply_text("❌ Invalid user ID!")


@admin_only
def viewlogs_command(update: Update, context: CallbackContext):
    logs = db.get_logs(limit=20)
    if not logs:
        update.message.reply_text("📋 No logs found.")
        return
    text = "📋 Recent Logs:\n\n"
    for log in logs:
        text += (
            f"[{log['timestamp'][:16]}] "
            f"{log['action']} | {log['user_id']}\n"
        )
        if log["details"]:
            text += f"  └─ {log['details'][:50]}\n"
    if len(text) > 4000:
        text = text[:4000] + "\n... (truncated)"
    update.message.reply_text(text)


@admin_only
def exportlogs_command(update: Update, context: CallbackContext):
    logs_text = db.export_logs()
    fname = f"logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(logs_text)
    update.message.reply_document(
        document=open(fname, "rb"),
        filename=fname,
        caption="📋 Exported Logs",
    )
    os.remove(fname)


@admin_only
def systemstats_command(update: Update, context: CallbackContext):
    stats = db.get_system_stats()
    maint = "ON" if db.get_setting("maintenance") == "1" else "OFF"
    ai_st = "OFF" if db.get_setting("ai_disabled") == "1" else "ON"
    update.message.reply_text(
        f"📊 System Stats:\n\n"
        f"👥 Users: {stats['total_users']}\n"
        f"💬 Messages: {stats['total_messages']}\n"
        f"🟢 Sessions: {stats['active_sessions']}\n"
        f"🔴 Banned: {stats['banned_users']}\n"
        f"👑 Admins: {stats['admin_count']}\n"
        f"🔧 Maintenance: {maint}\n"
        f"🤖 AI: {ai_st}\n"
        f"🔑 Keys: {len(GEMINI_API_KEYS)}"
    )


@admin_only
def memorystats_command(update: Update, context: CallbackContext):
    stats = db.get_memory_stats()
    update.message.reply_text(
        f"🧠 Memory Stats:\n\n"
        f"📝 Memories: {stats['total_memories']}\n"
        f"💬 Chat Records: {stats['total_chat_records']}\n"
        f"💾 DB Size: {stats['db_size_kb']} KB"
    )


@admin_only
def keystats_command(update: Update, context: CallbackContext):
    """Show API key usage statistics"""
    key_stats = db.get_api_key_stats()
    text = (
        f"🔑 API Key Stats\n"
        f"Total Keys: {len(GEMINI_API_KEYS)}\n"
        f"Model: {GEMINI_MODEL}\n\n"
    )

    if key_stats:
        for ks in key_stats:
            idx = ks["key_index"]
            masked = ""
            if idx < len(GEMINI_API_KEYS):
                k = GEMINI_API_KEYS[idx]
                masked = k[:6] + "..." + k[-4:]
            text += (
                f"Key #{idx} ({masked}):\n"
                f"  ✅ Calls: {ks['total_calls']}\n"
                f"  ❌ Errors: {ks['total_errors']}\n"
                f"  🕐 Last: {(ks['last_used'] or 'Never')[:16]}\n"
            )
            if ks["last_error"]:
                text += (
                    f"  ⚠️ Error: {ks['last_error'][:60]}\n"
                )
            text += "\n"
    else:
        text += "No usage data yet.\n"

    if len(text) > 4000:
        text = text[:4000] + "\n... (truncated)"

    update.message.reply_text(text)


@admin_only
def setphrase_command(update: Update, context: CallbackContext):
    if not context.args:
        current = db.get_setting("trigger_phrase") or "ruhi ji"
        update.message.reply_text(
            f"Current phrase: `{current}`\n"
            f"Usage: `/setphrase <phrase>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    phrase = " ".join(context.args).lower()
    db.set_setting("trigger_phrase", phrase)
    db.add_log(
        update.effective_user.id,
        "SET_PHRASE", f"New: {phrase}",
    )
    update.message.reply_text(
        f"✅ Phrase: `{phrase}`",
        parse_mode=ParseMode.MARKDOWN,
    )


@admin_only
def setprompt_command(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text(
            "Usage: `/setprompt <text>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    prompt = " ".join(context.args)
    db.set_setting("custom_prompt", prompt)
    db.add_log(
        update.effective_user.id,
        "SET_PROMPT", "Updated",
    )
    update.message.reply_text("✅ Custom prompt updated!")


@admin_only
def toggleai_command(update: Update, context: CallbackContext):
    current = db.get_setting("ai_disabled")
    new_val = "0" if current == "1" else "1"
    db.set_setting("ai_disabled", new_val)
    status = "ON" if new_val == "0" else "OFF"
    update.message.reply_text(f"🤖 AI: {status}")


@admin_only
def setcontext_command(update: Update, context: CallbackContext):
    if not context.args:
        current = db.get_setting("max_context") or "15"
        update.message.reply_text(
            f"Current: {current}\n"
            f"Usage: `/setcontext <1-50>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        num = int(context.args[0])
        if not 1 <= num <= 50:
            update.message.reply_text("❌ Range: 1-50!")
            return
        db.set_setting("max_context", str(num))
        update.message.reply_text(f"✅ Context: {num}")
    except ValueError:
        update.message.reply_text("❌ Invalid number!")


@admin_only
def badwords_command(update: Update, context: CallbackContext):
    words = db.get_badwords()
    if words:
        update.message.reply_text(
            "🚫 Bad Words:\n\n"
            + "\n".join(f"• {w}" for w in words)
        )
    else:
        update.message.reply_text("🚫 No bad words set.")


@admin_only
def addbadword_command(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text(
            "Usage: `/addbadword <word>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    word = " ".join(context.args).lower()
    db.add_badword(word)
    db.add_log(
        update.effective_user.id,
        "ADD_BADWORD", f"Added: {word}",
    )
    update.message.reply_text(f"✅ Added: `{word}`",
                               parse_mode=ParseMode.MARKDOWN)


@admin_only
def removebadword_command(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text(
            "Usage: `/removebadword <word>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    word = " ".join(context.args).lower()
    db.remove_badword(word)
    db.add_log(
        update.effective_user.id,
        "REMOVE_BADWORD", f"Removed: {word}",
    )
    update.message.reply_text(f"✅ Removed: `{word}`",
                               parse_mode=ParseMode.MARKDOWN)


@admin_only
def viewhistory_command(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text(
            "Usage: `/viewhistory <user_id>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        target_id = int(context.args[0])
        history = db.get_history_text(target_id, target_id, 30)
        if history:
            if len(history) > 4000:
                history = history[:4000] + "\n... (truncated)"
            update.message.reply_text(
                f"📜 History for {target_id}:\n\n{history}"
            )
        else:
            update.message.reply_text("No history found.")
    except ValueError:
        update.message.reply_text("❌ Invalid user ID!")


@admin_only
def deletehistory_command(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text(
            "Usage: `/deletehistory <user_id>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        target_id = int(context.args[0])
        db.clear_chat_history(target_id)
        db.add_log(
            update.effective_user.id,
            "DEL_HISTORY", f"Deleted: {target_id}",
        )
        update.message.reply_text(
            f"🗑️ History deleted for {target_id}!"
        )
    except ValueError:
        update.message.reply_text("❌ Invalid user ID!")


@admin_only
def forcesummary_command(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text(
            "Usage: `/forcesummary <user_id>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        target_id = int(context.args[0])
        history = db.get_history_text(target_id, target_id, 50)
        if not history:
            update.message.reply_text("No history found.")
            return
        prompt = f"Summarize this conversation briefly:\n\n{history}"
        reply = call_gemini_with_rotation(
            "You are a summarizer.", [], prompt
        )
        if reply:
            update.message.reply_text(
                f"📋 Summary for {target_id}:\n\n{reply}"
            )
        else:
            update.message.reply_text("❌ Could not summarize.")
    except Exception as e:
        update.message.reply_text(f"❌ Error: {str(e)[:100]}")


@admin_only
def debugmode_command(update: Update, context: CallbackContext):
    current = db.get_setting("debug_mode")
    new_val = "0" if current == "1" else "1"
    db.set_setting("debug_mode", new_val)
    status = "ON" if new_val == "1" else "OFF"
    update.message.reply_text(f"🔧 Debug: {status}")


# ═══════════════════════════════════════════════════════════════
#  CALLBACK QUERY HANDLER (Inline Buttons)
# ═══════════════════════════════════════════════════════════════

def callback_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "home":
        keyboard = [
            [
                InlineKeyboardButton(
                    "📖 Help", callback_data="help"
                ),
                InlineKeyboardButton(
                    "👤 Profile", callback_data="profile"
                ),
            ],
            [
                InlineKeyboardButton(
                    "⚙️ Settings", callback_data="settings"
                ),
                InlineKeyboardButton(
                    "📊 Usage", callback_data="usage"
                ),
            ],
            [
                InlineKeyboardButton(
                    "👨‍💻 Developer",
                    url="https://t.me/RUHI_VIG_QNR",
                )
            ],
        ]
        query.edit_message_text(
            START_TEXT,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data == "help":
        keyboard = [
            [
                InlineKeyboardButton(
                    "👤 User Cmds", callback_data="user_cmds"
                ),
                InlineKeyboardButton(
                    "🔐 Admin Cmds", callback_data="admin_cmds"
                ),
            ],
            [InlineKeyboardButton("🏠 Home", callback_data="home")],
        ]
        query.edit_message_text(
            HELP_TEXT,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data == "user_cmds":
        keyboard = [
            [
                InlineKeyboardButton(
                    "🔙 Back", callback_data="help"
                ),
                InlineKeyboardButton(
                    "🏠 Home", callback_data="home"
                ),
            ]
        ]
        query.edit_message_text(
            USER_COMMANDS_TEXT,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "admin_cmds":
        if not db.is_admin(user_id) and user_id != OWNER_ID:
            query.answer("⛔ Admin only!", show_alert=True)
            return
        keyboard = [
            [
                InlineKeyboardButton(
                    "🔙 Back", callback_data="help"
                ),
                InlineKeyboardButton(
                    "🏠 Home", callback_data="home"
                ),
            ]
        ]
        query.edit_message_text(
            ADMIN_COMMANDS_TEXT,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN,
        )

    elif data == "profile":
        user = query.from_user
        db_user = db.get_user(user.id)
        if not db_user:
            db.add_user(
                user.id, user.username,
                user.first_name, user.last_name or "",
            )
            db_user = db.get_user(user.id)
        memories = db.get_all_memory(user.id)
        banned = (
            "🟢 Active"
            if not db_user["is_banned"]
            else "🔴 Banned"
        )
        profile_text = (
            f"╭───────────────────⦿\n"
            f"│ 👤 ᴜsᴇʀ ᴘʀᴏғɪʟᴇ\n"
            f"├───────────────────⦿\n"
            f"│ ɴᴀᴍᴇ: {db_user['first_name']}\n"
            f"│ ɪᴅ: {db_user['user_id']}\n"
            f"│ ᴍᴇssᴀɢᴇs: {db_user['total_messages']}\n"
            f"│ ᴍᴇᴍᴏʀɪᴇs: {len(memories)}\n"
            f"│ ᴍᴏᴏᴅ: {db_user['mood']}\n"
            f"│ sᴛᴀᴛᴜs: {banned}\n"
            f"╰───────────────────⦿"
        )
        keyboard = [
            [InlineKeyboardButton("🏠 Home", callback_data="home")]
        ]
        query.edit_message_text(
            profile_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data == "settings":
        keyboard = [
            [
                InlineKeyboardButton(
                    "🎭 Mode", callback_data="settings_mode"
                ),
                InlineKeyboardButton(
                    "🌐 Lang", callback_data="settings_lang"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🧹 Clear", callback_data="settings_clear"
                ),
                InlineKeyboardButton(
                    "🔄 Reset", callback_data="settings_reset"
                ),
            ],
            [InlineKeyboardButton("🏠 Home", callback_data="home")],
        ]
        query.edit_message_text(
            "⚙️ Settings:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data == "settings_mode":
        keyboard = [
            [
                InlineKeyboardButton(
                    "😊 Sweet", callback_data="mode_sweet"
                ),
                InlineKeyboardButton(
                    "🧠 Smart", callback_data="mode_smart"
                ),
            ],
            [
                InlineKeyboardButton(
                    "😂 Funny", callback_data="mode_funny"
                ),
                InlineKeyboardButton(
                    "📚 Pro", callback_data="mode_professional"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔙 Back", callback_data="settings"
                )
            ],
        ]
        query.edit_message_text(
            "🎭 Choose personality:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data.startswith("mode_"):
        mode = data.replace("mode_", "")
        db.update_user(user_id, personality=mode)
        query.answer(f"✅ Mode: {mode}", show_alert=True)
        keyboard = [
            [InlineKeyboardButton("🏠 Home", callback_data="home")]
        ]
        query.edit_message_text(
            f"🎭 Personality: {mode.capitalize()} ✅",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data == "settings_lang":
        keyboard = [
            [
                InlineKeyboardButton(
                    "🇮🇳 Hinglish",
                    callback_data="lang_hinglish",
                ),
                InlineKeyboardButton(
                    "🇮🇳 Hindi", callback_data="lang_hindi"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🇬🇧 English",
                    callback_data="lang_english",
                ),
                InlineKeyboardButton(
                    "🌍 Auto", callback_data="lang_auto"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔙 Back", callback_data="settings"
                )
            ],
        ]
        query.edit_message_text(
            "🌐 Choose language:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data.startswith("lang_"):
        lang = data.replace("lang_", "")
        db.update_user(user_id, language=lang)
        query.answer(f"✅ Language: {lang}", show_alert=True)
        keyboard = [
            [InlineKeyboardButton("🏠 Home", callback_data="home")]
        ]
        query.edit_message_text(
            f"🌐 Language: {lang.capitalize()} ✅",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data == "settings_clear":
        chat_id = query.message.chat_id
        db.clear_chat_history(user_id, chat_id)
        db.deactivate_session(user_id, chat_id)
        query.answer("🧹 Cleared!", show_alert=True)
        keyboard = [
            [InlineKeyboardButton("🏠 Home", callback_data="home")]
        ]
        query.edit_message_text(
            '🧹 Memory cleared! Say "Ruhi ji" 🥀',
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data == "settings_reset":
        chat_id = query.message.chat_id
        db.clear_chat_history(user_id, chat_id)
        db.deactivate_session(user_id, chat_id)
        db.clear_memory(user_id)
        query.answer("🔄 Reset done!", show_alert=True)
        keyboard = [
            [InlineKeyboardButton("🏠 Home", callback_data="home")]
        ]
        query.edit_message_text(
            "🔄 Everything reset! Fresh start 🥀",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data == "usage":
        db_user = db.get_user(user_id)
        if db_user:
            usage_text = (
                f"📊 Your Usage:\n\n"
                f"💬 Messages: {db_user['total_messages']}\n"
                f"🧠 Memories: "
                f"{len(db.get_all_memory(user_id))}\n"
                f"📅 Joined: {db_user['first_seen'][:10]}"
            )
        else:
            usage_text = "No data found."
        keyboard = [
            [InlineKeyboardButton("🏠 Home", callback_data="home")]
        ]
        query.edit_message_text(
            usage_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


# ═══════════════════════════════════════════════════════════════
#  MAIN MESSAGE HANDLER (AI Chat Logic)
# ═══════════════════════════════════════════════════════════════

def handle_message(update: Update, context: CallbackContext):
    if not update.message or not update.message.text:
        return

    message = update.message
    text = message.text.strip()
    user = message.from_user
    user_id = user.id
    chat_id = message.chat_id
    is_group = message.chat.type in ("group", "supergroup")

    if text.startswith("/"):
        return

    # Maintenance
    if db.get_setting("maintenance") == "1":
        if user_id != OWNER_ID and not db.is_admin(user_id):
            return

    # Banned
    if db.is_banned(user_id):
        return

    # AI disabled
    if db.get_setting("ai_disabled") == "1":
        return

    # Register user
    db.add_user(
        user_id, user.username,
        user.first_name, user.last_name or "",
    )

    # Trigger phrase
    trigger_phrase = (
        db.get_setting("trigger_phrase") or "ruhi ji"
    )
    text_lower = text.lower()
    contains_trigger = trigger_phrase in text_lower
    session_active = db.is_session_active(user_id, chat_id)

    # ── TRIGGER PRESENT ──
    if contains_trigger:
        db.activate_session(user_id, chat_id)
        db.increment_messages(user_id)

        # Clean trigger phrase from message
        clean_message = text
        for variant in [
            trigger_phrase,
            trigger_phrase.title(),
            trigger_phrase.upper(),
            trigger_phrase.capitalize(),
        ]:
            clean_message = clean_message.replace(
                variant, ""
            ).strip()
        if not clean_message:
            clean_message = "hello"

        # Bad words check
        if db.contains_badword(text):
            message.reply_text(
                "Arey! Acche se baat karo na 🥺"
            )
            db.add_log(
                user_id, "BADWORD",
                f"Detected: {text[:50]}",
            )
            return

        detect_address(user_id, text_lower)
        context.bot.send_chat_action(
            chat_id=chat_id, action="typing"
        )

        user_name = user.first_name or "dear"
        reply = get_gemini_response(
            user_id, chat_id, clean_message, user_name
        )
        message.reply_text(reply)
        db.add_log(
            user_id, "CHAT", f"Triggered: {text[:50]}"
        )

    # ── SESSION ACTIVE + PRIVATE ──
    elif session_active and not is_group:
        db.refresh_session(user_id, chat_id)
        db.increment_messages(user_id)

        if db.contains_badword(text):
            message.reply_text("Acche se baat karo na 🥺")
            return

        detect_address(user_id, text_lower)
        context.bot.send_chat_action(
            chat_id=chat_id, action="typing"
        )

        user_name = user.first_name or "dear"
        reply = get_gemini_response(
            user_id, chat_id, text, user_name
        )
        message.reply_text(reply)

    # ── SESSION ACTIVE + GROUP (reply to bot) ──
    elif session_active and is_group:
        if (
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id
            == context.bot.id
        ):
            db.refresh_session(user_id, chat_id)
            db.increment_messages(user_id)

            if db.contains_badword(text):
                message.reply_text("Acche se baat karo na 🥺")
                return

            detect_address(user_id, text_lower)
            context.bot.send_chat_action(
                chat_id=chat_id, action="typing"
            )

            user_name = user.first_name or "dear"
            reply = get_gemini_response(
                user_id, chat_id, text, user_name
            )
            message.reply_text(reply)


def detect_address(user_id, text_lower):
    if "didi" in text_lower:
        db.update_user(user_id, preferred_address="didi")
        db.set_memory(user_id, "address_style", "didi")
    elif "jaan" in text_lower:
        db.update_user(user_id, preferred_address="jaan")
        db.set_memory(user_id, "address_style", "jaan")
    elif "madam" in text_lower:
        db.update_user(user_id, preferred_address="madam")
        db.set_memory(user_id, "address_style", "madam")
    elif "baby" in text_lower or "babe" in text_lower:
        db.update_user(user_id, preferred_address="sweet")
        db.set_memory(user_id, "address_style", "sweet")
    elif "bro" in text_lower or "bhai" in text_lower:
        db.update_user(user_id, preferred_address="bro")
        db.set_memory(user_id, "address_style", "bro")


# ═══════════════════════════════════════════════════════════════
#  ERROR HANDLER
# ═══════════════════════════════════════════════════════════════

def error_handler(update: Update, context: CallbackContext):
    logger.error(f"Error: {context.error}")
    try:
        if update and update.effective_message:
            update.effective_message.reply_text(
                "Oops! Kuch problem ho gaya 🥺\n"
                "Thodi der baad try karo..."
            )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

def main():
    logger.info("🚀 Starting Ruhi ji Bot...")
    logger.info(f"🔑 API Keys: {len(GEMINI_API_KEYS)}")
    logger.info(f"📦 Model: {GEMINI_MODEL}")

    # Initialize owner
    db.add_user(OWNER_ID, "owner", "Owner", "")
    db.add_admin(OWNER_ID)

    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    # ── User Commands ──
    dp.add_handler(CommandHandler("start", start_command))
    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(CommandHandler("profile", profile_command))
    dp.add_handler(CommandHandler("clear", clear_command))
    dp.add_handler(CommandHandler("mode", mode_command))
    dp.add_handler(CommandHandler("lang", lang_command))
    dp.add_handler(
        CommandHandler("personality", personality_command)
    )
    dp.add_handler(CommandHandler("usage", usage_command))
    dp.add_handler(CommandHandler("summary", summary_command))
    dp.add_handler(CommandHandler("reset", reset_command))

    # ── Admin Commands ──
    dp.add_handler(CommandHandler("admin", admin_command))
    dp.add_handler(CommandHandler("addadmin", addadmin_command))
    dp.add_handler(
        CommandHandler("removeadmin", removeadmin_command)
    )
    dp.add_handler(
        CommandHandler("broadcast", broadcast_command)
    )
    dp.add_handler(
        CommandHandler("totalusers", totalusers_command)
    )
    dp.add_handler(
        CommandHandler("activeusers", activeusers_command)
    )
    dp.add_handler(
        CommandHandler("forceclear", forceclear_command)
    )
    dp.add_handler(CommandHandler("shutdown", shutdown_command))
    dp.add_handler(CommandHandler("restart", restart_command))
    dp.add_handler(
        CommandHandler("maintenance", maintenance_command)
    )
    dp.add_handler(CommandHandler("ban", ban_command))
    dp.add_handler(CommandHandler("unban", unban_command))
    dp.add_handler(CommandHandler("viewlogs", viewlogs_command))
    dp.add_handler(
        CommandHandler("exportlogs", exportlogs_command)
    )
    dp.add_handler(
        CommandHandler("systemstats", systemstats_command)
    )
    dp.add_handler(
        CommandHandler("memorystats", memorystats_command)
    )
    dp.add_handler(CommandHandler("keystats", keystats_command))
    dp.add_handler(
        CommandHandler("setphrase", setphrase_command)
    )
    dp.add_handler(
        CommandHandler("setprompt", setprompt_command)
    )
    dp.add_handler(CommandHandler("toggleai", toggleai_command))
    dp.add_handler(
        CommandHandler("setcontext", setcontext_command)
    )
    dp.add_handler(CommandHandler("badwords", badwords_command))
    dp.add_handler(
        CommandHandler("addbadword", addbadword_command)
    )
    dp.add_handler(
        CommandHandler("removebadword", removebadword_command)
    )
    dp.add_handler(
        CommandHandler("viewhistory", viewhistory_command)
    )
    dp.add_handler(
        CommandHandler("deletehistory", deletehistory_command)
    )
    dp.add_handler(
        CommandHandler("forcesummary", forcesummary_command)
    )
    dp.add_handler(
        CommandHandler("debugmode", debugmode_command)
    )

    # ── Callbacks ──
    dp.add_handler(CallbackQueryHandler(callback_handler))

    # ── Messages ──
    dp.add_handler(
        MessageHandler(
            Filters.text & ~Filters.command,
            handle_message,
        )
    )

    # ── Errors ──
    dp.add_error_handler(error_handler)

    # ── Flask keep-alive ──
    flask_thread = threading.Thread(
        target=lambda: flask_app.run(
            host="0.0.0.0", port=PORT
        ),
        daemon=True,
    )
    flask_thread.start()
    logger.info(f"🌐 Flask on port {PORT}")

    # ── Start polling ──
    # drop_pending_updates=True fixes "Conflict" error
    # when multiple instances try to poll
    logger.info("🤖 Starting polling...")
    updater.start_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )
    updater.idle()


if __name__ == "__main__":
    main()
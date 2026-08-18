import os
import sys
import logging
import re
import sqlite3
from contextlib import contextmanager
from functools import partial
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

def load_env(path=".env"):
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()

# ===================== SETTINGS =====================
TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = -1002223169314
ADMIN_IDS = [1089153788, 1404025641]

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Отключаем лишние логи httpx и apscheduler
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

# ===================== DATABASE =====================
@contextmanager
def db():
    conn = sqlite3.connect("proposals.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                proposal_type TEXT NOT NULL,
                proposal_text TEXT,
                file_id TEXT,
                status TEXT DEFAULT 'pending',
                is_anonymous INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_seen TEXT NOT NULL
            )
        """)
        for admin in ADMIN_IDS:
            conn.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (admin,))


def get_admins():
    with db() as conn:
        return [r["user_id"] for r in conn.execute("SELECT user_id FROM admins")]


def add_user(user_id, username):
    with db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO users(user_id, username, first_seen)
            VALUES (?, ?, ?)
        """, (user_id, username or "", datetime.now(timezone.utc).isoformat()))


def add_proposal(user_id, username, ptype, text=None, file_id=None):
    add_user(user_id, username)
    with db() as conn:
        cur = conn.execute("""
            INSERT INTO proposals(user_id, username, proposal_type, proposal_text, file_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, username or "", ptype, text, file_id, datetime.now(timezone.utc).isoformat()))
        return cur.lastrowid


def update_status(pid, status, anon=False):
    with db() as conn:
        conn.execute("UPDATE proposals SET status=?, is_anonymous=? WHERE id=?", (status, int(anon), pid))


def claim_pending(pid, status, anon=False):
    with db() as conn:
        cur = conn.execute(
            "UPDATE proposals SET status=?, is_anonymous=? WHERE id=? AND status='pending'",
            (status, int(anon), pid)
        )
        return cur.rowcount == 1


# ===================== KEYBOARDS =====================
def review_kb(pid: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{pid}"),
            InlineKeyboardButton("👤 Анонимно", callback_data=f"approve_anon_{pid}")
        ],
        [InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{pid}")]
    ])


# ===================== NOTIFY ADMINS =====================
async def notify_admins(context, text, media_type=None, file_id=None, kb=None):
    for admin in get_admins():
        try:
            if media_type and file_id:
                sender = getattr(context.bot, f"send_{media_type}")
                await sender(admin, **{media_type: file_id}, caption=text[:1024], reply_markup=kb)
            else:
                await context.bot.send_message(admin, text[:4096], reply_markup=kb)
        except Exception as e:
            logger.warning("Admin notify failed %s: %s", admin, e)


# ===================== HANDLERS =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📤 Отправить предложение", callback_data="send")]])
    await update.message.reply_text("Добро пожаловать! Отправьте предложение 👇", reply_markup=kb)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.message.from_user.id not in get_admins():
        return
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]
        pend = conn.execute("SELECT COUNT(*) FROM proposals WHERE status='pending'").fetchone()[0]
        app = conn.execute("SELECT COUNT(*) FROM proposals WHERE status='approved'").fetchone()[0]
        rej = conn.execute("SELECT COUNT(*) FROM proposals WHERE status='rejected'").fetchone()[0]
    await update.message.reply_text(f"📊 Статистика\n\nВсего: {total}\n⏳ {pend}\n✅ {app}\n❌ {rej}")


async def send_pending_proposal(context, chat_id, row):
    username = row["username"] or ""
    author = f"@{username}" if username else f"id {row['user_id']}"
    text = (
        f"🕓 #{row['id']} ожидает модерации\n"
        f"От: {author}\n"
        f"Тип: {row['proposal_type']}\n"
        f"Дата: {row['created_at']}\n\n"
        f"{row['proposal_text'] or ''}"
    )
    ctype = row["proposal_type"]
    file_id = row["file_id"]

    if ctype == "text" or not file_id:
        await context.bot.send_message(chat_id, text[:4096], reply_markup=review_kb(row["id"]))
        return

    try:
        sender = getattr(context.bot, f"send_{ctype}")
        await sender(chat_id, **{ctype: file_id}, caption=text[:1024], reply_markup=review_kb(row["id"]))
    except Exception as e:
        logger.warning("Pending proposal media send failed %s: %s", row["id"], e)
        fallback = f"{text}\n\nfile_id: {file_id}"
        await context.bot.send_message(chat_id, fallback[:4096], reply_markup=review_kb(row["id"]))


async def list_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.message.from_user.id not in get_admins():
        return

    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM proposals WHERE status='pending' ORDER BY id ASC"
        ).fetchall()

    if not rows:
        await update.message.reply_text("Ожидающих предложений нет.")
        return

    await update.message.reply_text(f"Ожидают модерации: {len(rows)}")
    for row in rows:
        await send_pending_proposal(context, update.message.chat_id, row)


async def fulltest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.message.from_user.id not in get_admins():
        return
    results = []

    try:
        me = await context.bot.get_me()
        results.append(f"✅ Telegram API доступен: @{me.username}")
    except Exception as e:
        results.append(f"❌ Telegram API ошибка: {e}")

    try:
        with db() as conn:
            conn.execute("SELECT 1")
        results.append("✅ БД доступна")
    except Exception as e:
        logger.warning("DB self-test failed: %s", e, exc_info=True)
        results.append(f"❌ БД недоступна: {e}")

    try:
        msg = await context.bot.send_message(CHANNEL_ID, "Тест сообщения для self-test (будет удалено)")
        try:
            await context.bot.delete_message(CHANNEL_ID, msg.message_id)
            results.append("✅ Публикация текста в канал работает, тестовое сообщение удалено")
        except Exception as e:
            results.append(f"⚠️ Публикация текста работает, но удалить тест не удалось: {e}")
    except Exception as e:
        results.append(f"❌ Канал ошибка: {e}")

    await update.message.reply_text("📊 Self-test:\n" + "\n".join(results))


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return
    u = update.message.from_user
    author = f"@{u.username}" if u.username else f"id {u.id}"
    pid = add_proposal(u.id, u.username, "text", update.message.text)
    await notify_admins(context, f"📝 #{pid} от {author}\n\n{update.message.text}", kb=review_kb(pid))
    await update.message.reply_text("✅ Отправлено на модерацию")


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE, media_type: str):
    if not update.message or not update.message.from_user:
        return

    u = update.message.from_user
    media = getattr(update.message, media_type)

    if not media:
        return

    if isinstance(media, (list, tuple)):
        file_id = media[-1].file_id
    else:
        file_id = media.file_id

    caption = update.message.caption or ""
    author = f"@{u.username}" if u.username else f"id {u.id}"
    pid = add_proposal(u.id, u.username, media_type, caption, file_id)

    await notify_admins(
        context,
        f"📎 #{pid} от {author}\n\n{caption}",
        media_type,
        file_id,
        review_kb(pid)
    )

    await update.message.reply_text("✅ Отправлено на модерацию")


async def edit_review_message(message, ctype, text):
    if not message:
        return
    if ctype == "text":
        await message.edit_text(text)
    else:
        await message.edit_caption(text)


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return

    if q.data == "send":
        await q.answer()
        if q.message:
            await q.message.edit_text("📤 Отправьте текст / фото / видео / файл")
        return

    if q.from_user.id not in get_admins():
        await q.answer("Нет доступа", show_alert=True)
        return

    await q.answer()

    m = re.match(r"(approve|approve_anon|reject)_(\d+)", q.data)
    if not m:
        return

    action, pid = m.groups()
    pid = int(pid)

    with db() as conn:
        p = conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()

    if not p or p["status"] != "pending":
        if q.message:
            await edit_review_message(q.message, p["proposal_type"] if p else "text", "⚠️ Уже обработано")
        return

    anon = action == "approve_anon"
    ctype = p["proposal_type"]
    author = f"@{p['username']}" if p["username"] else f"id {p['user_id']}"
    text = (p["proposal_text"] or "") + ("" if anon else f"\n\nОт {author}")

    try:
        if action.startswith("approve"):
            if not claim_pending(pid, "processing", anon):
                if q.message:
                    await edit_review_message(q.message, ctype, "⚠️ Уже обработано")
                return

            if ctype == "text":
                await context.bot.send_message(CHANNEL_ID, text[:4096])
                update_status(pid, "approved", anon)
                if q.message:
                    try:
                        await q.message.edit_text("✅ Опубликовано")
                    except Exception as e:
                        logger.warning("Review message edit failed: %s", e)
            else:
                sender = getattr(context.bot, f"send_{ctype}")
                await sender(CHANNEL_ID, **{ctype: p["file_id"]}, caption=text[:1024])
                update_status(pid, "approved", anon)
                if q.message:
                    try:
                        await q.message.edit_caption("✅ Опубликовано")
                    except Exception as e:
                        logger.warning("Review message edit failed: %s", e)

            try:
                user_text = "🎉 Опубликовано анонимно!" if anon else "🎉 Опубликовано!"
                await context.bot.send_message(p["user_id"], user_text)
            except Exception as e:
                logger.warning("User publish notification failed %s: %s", p["user_id"], e)

        elif action == "reject":
            if not claim_pending(pid, "rejected"):
                if q.message:
                    await edit_review_message(q.message, ctype, "⚠️ Уже обработано")
                return

            if q.message:
                await edit_review_message(q.message, ctype, "❌ Отклонено")
            try:
                await context.bot.send_message(p["user_id"], "😔 Отклонено")
            except Exception as e:
                logger.warning("User reject notification failed %s: %s", p["user_id"], e)

    except Exception as e:
        if action.startswith("approve"):
            update_status(pid, "pending", anon)
        logger.error("Ошибка callback: %s", e, exc_info=True)
        if q.message:
            await edit_review_message(q.message, ctype, f"❌ Ошибка: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    exc_info = None
    if context.error:
        exc_info = (type(context.error), context.error, context.error.__traceback__)
    logger.error("Unhandled error", exc_info=exc_info)
    if ADMIN_IDS:
        try:
            await context.bot.send_message(ADMIN_IDS[0], f"🚨 Ошибка:\n{context.error}")
        except Exception as e:
            logger.warning("Admin error notification failed: %s", e)


# ===================== MAIN =====================
def main():
    if not TOKEN:
        raise RuntimeError("Set BOT_TOKEN or TELEGRAM_BOT_TOKEN environment variable")

    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("list", list_pending))
    app.add_handler(CommandHandler("fulltest", fulltest))
    app.add_handler(CallbackQueryHandler(callbacks))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, partial(handle_media, media_type="photo")))
    app.add_handler(MessageHandler(filters.VIDEO, partial(handle_media, media_type="video")))
    app.add_handler(MessageHandler(filters.VOICE, partial(handle_media, media_type="voice")))
    app.add_handler(MessageHandler(filters.Document.ALL, partial(handle_media, media_type="document")))

    app.add_error_handler(error_handler)

    logger.info("🚀 Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()

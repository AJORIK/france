
import logging
import os
from typing import Optional

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# -----------------------------
# Настройки
# -----------------------------
TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# -----------------------------
# Сообщения после /start
# -----------------------------
PROMO_MESSAGE_1 = """Pour confirmer votre participation, abonnez-vous à ma chaîne :

(Les résultats du tirage au sort y seront publiés)

https://t.me/+qEekFVifSrxiMThi

Envoyez-moi également en message privé la phrase :

« Je participe », ainsi que votre prénom et votre nom pour la liste des participants.

@EtenneBoucher"""

PROMO_MESSAGE_2 = """Vous avez 5 minutes pour m'envoyer un message privé, sinon votre participation sera annulée."""

PROMO_SECOND_DELAY_SECONDS = 60

ADMINS = ["suerde", "fbtraffick"]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

broadcast_data = {}
deactivate_pending = set()

# -----------------------------
# PostgreSQL
# -----------------------------
if not DATABASE_URL:
    raise RuntimeError("Не найден DATABASE_URL!")

conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
with conn.cursor() as cur:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id BIGINT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_daily_sent_at TIMESTAMPTZ,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            start_count INT NOT NULL DEFAULT 0,
            username TEXT,
            first_name TEXT
        );
        """
    )
    conn.commit()

# -----------------------------
# Вспомогательные функции
# -----------------------------
def upsert_chat_db(chat_id: int, username: Optional[str], first_name: Optional[str]) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO subscribers (chat_id, start_count, username, first_name)
            VALUES (%s, 1, %s, %s)
            ON CONFLICT (chat_id)
            DO UPDATE SET start_count = subscribers.start_count + 1,
                          username = EXCLUDED.username,
                          first_name = EXCLUDED.first_name,
                          is_active = TRUE
            RETURNING start_count;
            """,
            (chat_id, username, first_name),
        )
        res = cur.fetchone()
        conn.commit()
        return bool(res and res["start_count"] == 1)


def get_active_subscribers():
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM subscribers WHERE is_active = TRUE ORDER BY created_at DESC;")
        return cur.fetchall()


def deactivate(chat_id: int):
    with conn.cursor() as cur:
        cur.execute("UPDATE subscribers SET is_active = FALSE WHERE chat_id = %s;", (chat_id,))
        conn.commit()


def is_admin(username: Optional[str]) -> bool:
    return bool(username and username in ADMINS)


async def send_promo_message(
    application: Application,
    chat_id: int,
    message: str,
) -> bool:
    try:
        await application.bot.send_message(
            chat_id=chat_id,
            text=message,
            disable_web_page_preview=True,
        )
        return True
    except Exception as exc:
        logger.warning("Не удалось отправить промо chat_id=%s: %s", chat_id, exc)
        deactivate(chat_id)
        return False


async def send_followup_promos(application: Application, chat_id: int):
    # Второе сообщение — через 1 минуту после /start.
    await asyncio.sleep(PROMO_SECOND_DELAY_SECONDS)
    await send_promo_message(application, chat_id, PROMO_MESSAGE_2)


# -----------------------------
# /start
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return

    upsert_chat_db(chat.id, user.username, user.first_name)

    # Первое сообщение — сразу после /start.
    sent = await send_promo_message(
        context.application,
        chat.id,
        PROMO_MESSAGE_1,
    )
    if not sent:
        return

    # Второе сообщение отправляется отдельной задачей через 1 минуту,
    # чтобы бот продолжал работать для других пользователей.
    context.application.create_task(
        send_followup_promos(context.application, chat.id),
        update=update,
        name=f"promo_followups_{chat.id}",
    )


# -----------------------------
# Админ-меню
# -----------------------------
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    if not user or not message:
        return

    if not is_admin(user.username):
        await message.reply_text("У вас нет прав")
        return

    keyboard = [
        [InlineKeyboardButton("📊 Отправить всем промо", callback_data="send_all")],
        [InlineKeyboardButton("📋 Статистика пользователей", callback_data="stats")],
        [InlineKeyboardButton("👥 Список активных подписчиков", callback_data="list_active")],
        [InlineKeyboardButton("✉️ Создать рассылку", callback_data="broadcast")],
        [InlineKeyboardButton("❌ Деактивировать пользователя", callback_data="deactivate")],
    ]

    await message.reply_text("🛠 Админ-меню", reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if not query or not user:
        return

    if not is_admin(user.username):
        await query.answer("Нет прав", show_alert=True)
        return

    data = query.data

    if data == "send_all":
        await query.answer("Начинаю рассылку...")

        subscribers = get_active_subscribers()
        sent = 0
        failed = 0

        # Первое сообщение отправляем всем сразу.
        # Для каждого успешно получившего запускаем второе сообщение по таймеру.
        for record in subscribers:
            chat_id = int(record["chat_id"])
            ok = await send_promo_message(
                context.application,
                chat_id,
                PROMO_MESSAGE_1,
            )

            if ok:
                sent += 1
                context.application.create_task(
                    send_followup_promos(context.application, chat_id),
                    name=f"promo_followups_broadcast_{chat_id}",
                )
            else:
                failed += 1

        if query.message:
            await query.message.reply_text(
                f"✅ Первое промо отправлено: {sent}\n"
                f"❌ Не доставлено: {failed}\n\n"
                "Второе сообщение уйдёт через 1 минуту."
            )

    elif data == "stats":
        total_active = len(get_active_subscribers())
        await query.answer()

        if query.message:
            await query.message.reply_text(f"📋 Активных пользователей: {total_active}")

    elif data == "list_active":
        subscribers = get_active_subscribers()
        names = []

        for row in subscribers[:50]:
            username = row.get("username")
            first_name = row.get("first_name")
            chat_id = row.get("chat_id")

            if username:
                names.append(f"@{username} — {chat_id}")
            elif first_name:
                names.append(f"{first_name} — {chat_id}")
            else:
                names.append(str(chat_id))

        text = "👥 Активные подписчики:\n\n" + ("\n".join(names) if names else "Список пуст.")

        if len(subscribers) > 50:
            text += f"\n\n...и ещё {len(subscribers) - 50} пользователей"

        await query.answer()

        if query.message:
            await query.message.reply_text(text)

    elif data == "broadcast":
        broadcast_data[user.id] = {
            "message": None,
            "button_text": None,
            "url": None,
            "step": "await_post",
        }

        await query.answer()

        if query.message:
            await query.message.reply_text(
                "Перешлите боту готовый пост (текст / фото / видео / документ)."
            )

    elif data == "deactivate":
        deactivate_pending.add(user.id)
        await query.answer()

        if query.message:
            await query.message.reply_text("Введите chat_id пользователя для деактивации:")


async def admin_state_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message

    if not user or not msg or not is_admin(user.username):
        return

    # Деактивация пользователя.
    if user.id in deactivate_pending:
        deactivate_pending.discard(user.id)

        try:
            chat_id = int((msg.text or "").strip())
            deactivate(chat_id)
            await msg.reply_text(f"✅ Пользователь с chat_id {chat_id} деактивирован.")
        except ValueError:
            await msg.reply_text("❌ Ошибка: chat_id должен быть числом.")

        return

    # Обычная админская рассылка.
    if user.id not in broadcast_data:
        return

    data = broadcast_data[user.id]
    step = data.get("step")

    if step == "await_post":
        if msg.text or msg.caption or msg.photo or msg.video or msg.document:
            data["message"] = msg
            data["step"] = "await_button_text"
            await msg.reply_text(
                "✅ Пост принят. Введите текст кнопки или отправьте /skip, если кнопка не нужна."
            )
        else:
            await msg.reply_text("❌ Нужен текст, фото, видео или документ.")

        return

    if step == "await_button_text":
        text = (msg.text or "").strip()
        data["button_text"] = "" if text == "/skip" else text
        data["step"] = "await_url"
        await msg.reply_text("Введите URL для кнопки или отправьте /skip")
        return

    if step == "await_url":
        text = (msg.text or "").strip()
        data["url"] = "" if text == "/skip" else text

        keyboard = None
        if data["button_text"] and data["url"]:
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton(data["button_text"], url=data["url"])]]
            )

        sent_count = 0
        failed_count = 0
        original_msg = data["message"]

        for record in get_active_subscribers():
            chat_id = int(record["chat_id"])

            try:
                if original_msg.photo:
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=original_msg.photo[-1].file_id,
                        caption=original_msg.caption or "",
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                elif original_msg.video:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=original_msg.video.file_id,
                        caption=original_msg.caption or "",
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                elif original_msg.document:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=original_msg.document.file_id,
                        caption=original_msg.caption or "",
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                else:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=original_msg.text or "",
                        parse_mode="HTML",
                        disable_web_page_preview=False,
                        reply_markup=keyboard,
                    )

                sent_count += 1

            except Exception as exc:
                logger.warning("Ошибка рассылки chat_id=%s: %s", chat_id, exc)
                failed_count += 1

        del broadcast_data[user.id]

        await msg.reply_text(
            f"✅ Рассылка завершена.\nОтправлено: {sent_count}\nНе доставлено: {failed_count}"
        )


async def post_init(application: Application):
    await application.bot.set_my_commands([BotCommand("start", "Запустить бота")])


def main():
    if not TOKEN:
        raise RuntimeError("Не найден BOT_TOKEN!")

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_menu))
    app.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^(send_all|stats|list_active|broadcast|deactivate)$",
        )
    )
    app.add_handler(MessageHandler(~filters.COMMAND, admin_state_router))

    logger.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
